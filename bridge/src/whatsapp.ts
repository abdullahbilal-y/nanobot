/**
 * WhatsApp client wrapper using Baileys.
 * Based on OpenClaw's working implementation.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
import makeWASocket, {
  DisconnectReason,
  useMultiFileAuthState,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  downloadContentFromMessage,
} from '@whiskeysockets/baileys';

import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import pino from 'pino';
import { writeFile, mkdir } from 'fs/promises';
import { join } from 'path';
import { homedir } from 'os';

const VERSION = '0.1.0';

export interface InboundMessage {
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  fromMe?: boolean;
  /** Local path to a downloaded voice note, when the message carried one. */
  mediaPath?: string;
  mediaType?: string;
  durationSec?: number;
  /** True when this arrived via history sync/backfill rather than a live event. */
  historical?: boolean;
}

export interface WhatsAppClientOptions {
  authDir: string;
  onMessage: (msg: InboundMessage) => void;
  onQR: (qr: string) => void;
  onStatus: (status: string) => void;
}

/**
 * Rolling health counters for the media path.
 *
 * The socket happily reports "connected" while media downloads fail wholesale
 * after a vendor-side change, so connection state is not proof the integration
 * works. Count outcomes and expose them over the bridge instead.
 */
interface MediaHealth {
  attempts: number;
  successes: number;
  lastError: string | null;
  lastSuccessAt: number | null;
}

export class WhatsAppClient {
  private sock: any = null;
  private options: WhatsAppClientOptions;
  private reconnecting = false;
  private mediaDir: string;
  private health: MediaHealth = {
    attempts: 0,
    successes: 0,
    lastError: null,
    lastSuccessAt: null,
  };

  constructor(options: WhatsAppClientOptions) {
    this.options = options;
    this.mediaDir = join(homedir(), '.nanobot', 'media', 'voice');
  }

  async connect(): Promise<void> {
    const logger = pino({ level: 'silent' });
    const { state, saveCreds } = await useMultiFileAuthState(this.options.authDir);
    const { version } = await fetchLatestBaileysVersion();

    console.log(`Using Baileys version: ${version.join('.')}`);
    await mkdir(this.mediaDir, { recursive: true });

    // Create socket following OpenClaw's pattern
    this.sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      version,
      logger,
      printQRInTerminal: false,
      browser: ['nanobot', 'cli', VERSION],
      // Pull history on link so voice notes sent before the bridge was running
      // are still reachable for backfill.
      syncFullHistory: true,
      markOnlineOnConnect: false,
    });

    // Handle WebSocket errors
    if (this.sock.ws && typeof this.sock.ws.on === 'function') {
      this.sock.ws.on('error', (err: Error) => {
        console.error('WebSocket error:', err.message);
      });
    }

    // Handle connection updates
    this.sock.ev.on('connection.update', async (update: any) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Display QR code in terminal
        console.log('\n📱 Scan this QR code with WhatsApp (Linked Devices):\n');
        qrcode.generate(qr, { small: true });
        this.options.onQR(qr);
      }

      if (connection === 'close') {
        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
        const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

        console.log(`Connection closed. Status: ${statusCode}, Will reconnect: ${shouldReconnect}`);
        this.options.onStatus('disconnected');

        if (shouldReconnect && !this.reconnecting) {
          this.reconnecting = true;
          console.log('Reconnecting in 5 seconds...');
          setTimeout(() => {
            this.reconnecting = false;
            this.connect();
          }, 5000);
        }
      } else if (connection === 'open') {
        console.log('✅ Connected to WhatsApp');
        this.options.onStatus('connected');
      }
    });

    // Save credentials on update
    this.sock.ev.on('creds.update', saveCreds);

    // History sync — fires on initial link and in response to fetchMessageHistory().
    // Same message shape as live events, so both go through one path.
    this.sock.ev.on('messaging-history.set', async ({ messages }: { messages: any[] }) => {
      if (!messages?.length) return;
      console.log(`📜 History sync: ${messages.length} messages`);
      for (const msg of messages) {
        await this.emitMessage(msg, true);
      }
    });

    // Handle incoming messages
    this.sock.ev.on('messages.upsert', async ({ messages, type }: { messages: any[]; type: string }) => {
      if (type !== 'notify') return;

      for (const msg of messages) {
        await this.emitMessage(msg, false);
      }
    });
  }

  /** Normalize a live or historical message and hand it to the bridge. */
  private async emitMessage(msg: any, historical: boolean): Promise<void> {
    if (!msg?.key) return;
    if (msg.key.remoteJid === 'status@broadcast') return;

    const audio = msg.message?.audioMessage;
    let mediaPath: string | undefined;

    // Download the voice note before deciding whether there is content, so a
    // voice-only message still produces something useful downstream.
    if (audio) {
      mediaPath = (await this.downloadVoice(msg)) ?? undefined;
    }

    const content = this.extractMessageContent(msg);
    if (!content && !mediaPath) return;

    const isGroup = msg.key.remoteJid?.endsWith('@g.us') || false;

    this.options.onMessage({
      id: msg.key.id || '',
      sender: msg.key.remoteJid || '',
      pn: msg.key.remoteJidAlt || '',
      content: content || '[Voice Message]',
      timestamp: Number(msg.messageTimestamp) || 0,
      isGroup,
      fromMe: !!msg.key.fromMe,
      mediaPath,
      mediaType: audio ? 'voice' : undefined,
      durationSec: audio?.seconds ? Number(audio.seconds) : undefined,
      historical,
    });
  }

  /**
   * Download a voice note straight from the CDN and decrypt it locally.
   *
   * Baileys 7.x does not export a downloadMediaMessage() convenience wrapper,
   * so go at the primitives: downloadContentFromMessage() takes the mediaKey +
   * directPath the client already holds, fetches the ciphertext and decrypts
   * it. This is also the more durable path — the bundled fetch+validate+decrypt
   * helpers are what tend to break on vendor updates.
   */
  private async downloadVoice(msg: any): Promise<string | null> {
    const audio = msg.message?.audioMessage;
    if (!audio) return null;

    this.health.attempts++;
    try {
      const stream = await downloadContentFromMessage(
        {
          mediaKey: audio.mediaKey,
          directPath: audio.directPath,
          url: audio.url,
        } as any,
        'audio'
      );

      const chunks: Buffer[] = [];
      for await (const chunk of stream as any) {
        chunks.push(chunk as Buffer);
      }
      const buf = Buffer.concat(chunks);
      if (!buf.length) throw new Error('empty payload');

      const safeId = (msg.key.id || String(Date.now())).replace(/[^A-Za-z0-9_-]/g, '');
      const file = join(this.mediaDir, `${safeId}.ogg`);
      await writeFile(file, buf);

      this.health.successes++;
      this.health.lastSuccessAt = Date.now();
      console.log(`🎙️  Saved voice note ${safeId}.ogg (${buf.length} bytes)`);
      return file;
    } catch (error) {
      const message = (error as Error).message;
      this.health.lastError = message;
      // CDN media expires; an old voice note failing here is expected, and must
      // never take down ingestion of the message itself.
      console.error(`Voice download failed for ${msg.key.id}: ${message}`);
      return null;
    }
  }

  /**
   * Ask the phone to replay older history for a chat.
   * Results arrive asynchronously on the 'messaging-history.set' event.
   */
  async fetchHistory(jid: string, count = 50): Promise<string> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    const oldestKey = { remoteJid: jid, id: '', fromMe: false };
    const requestId = await this.sock.fetchMessageHistory(
      count,
      oldestKey,
      Math.floor(Date.now() / 1000)
    );
    console.log(`📜 Requested ${count} historical messages for ${jid} (req ${requestId})`);
    return requestId;
  }

  /** Outcome-based health, not just "is the socket up". */
  getHealth(): MediaHealth & { connected: boolean } {
    return { ...this.health, connected: !!this.sock };
  }

  private extractMessageContent(msg: any): string | null {
    const message = msg.message;
    if (!message) return null;

    // Text message
    if (message.conversation) {
      return message.conversation;
    }

    // Extended text (reply, link preview)
    if (message.extendedTextMessage?.text) {
      return message.extendedTextMessage.text;
    }

    // Image with caption
    if (message.imageMessage?.caption) {
      return `[Image] ${message.imageMessage.caption}`;
    }

    // Video with caption
    if (message.videoMessage?.caption) {
      return `[Video] ${message.videoMessage.caption}`;
    }

    // Document with caption
    if (message.documentMessage?.caption) {
      return `[Document] ${message.documentMessage.caption}`;
    }

    // Voice/Audio message
    if (message.audioMessage) {
      return `[Voice Message]`;
    }

    return null;
  }

  async sendMessage(to: string, text: string): Promise<void> {
    if (!this.sock) {
      throw new Error('Not connected');
    }

    await this.sock.sendMessage(to, { text });
  }

  async disconnect(): Promise<void> {
    if (this.sock) {
      this.sock.end(undefined);
      this.sock = null;
    }
  }
}
