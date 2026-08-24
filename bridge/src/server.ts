/**
 * WebSocket server for Python-Node.js bridge communication.
 */

import { WebSocketServer, WebSocket } from 'ws';
import { WhatsAppClient, InboundMessage } from './whatsapp.js';

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

interface BackfillCommand {
  type: 'backfill';
  jid: string;
  count?: number;
}

interface HealthCommand {
  type: 'health';
}

interface ContactsCommand {
  type: 'contacts';
  query?: string;
}

type BridgeCommand = SendCommand | BackfillCommand | HealthCommand | ContactsCommand;

interface BridgeMessage {
  type: 'message' | 'status' | 'qr' | 'error' | 'health' | 'contacts';
  [key: string]: unknown;
}

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private wa: WhatsAppClient | null = null;
  private clients: Set<WebSocket> = new Set();

  constructor(private port: number, private authDir: string) {}

  async start(): Promise<void> {
    // Create WebSocket server
    this.wss = new WebSocketServer({ port: this.port });
    console.log(`🌉 Bridge server listening on ws://localhost:${this.port}`);

    // Initialize WhatsApp client
    this.wa = new WhatsAppClient({
      authDir: this.authDir,
      onMessage: (msg) => this.broadcast({ type: 'message', ...msg }),
      onQR: (qr) => this.broadcast({ type: 'qr', qr }),
      onStatus: (status) => this.broadcast({ type: 'status', status }),
    });

    // Handle WebSocket connections
    this.wss.on('connection', (ws) => {
      console.log('🔗 Python client connected');
      this.clients.add(ws);

      ws.on('message', async (data) => {
        try {
          const cmd = JSON.parse(data.toString()) as BridgeCommand;
          const reply = await this.handleCommand(cmd);
          ws.send(JSON.stringify(reply));
        } catch (error) {
          console.error('Error handling command:', error);
          ws.send(JSON.stringify({ type: 'error', error: String(error) }));
        }
      });

      ws.on('close', () => {
        console.log('🔌 Python client disconnected');
        this.clients.delete(ws);
      });

      ws.on('error', (error) => {
        console.error('WebSocket error:', error);
        this.clients.delete(ws);
      });
    });

    // Connect to WhatsApp
    await this.wa.connect();
  }

  private async handleCommand(cmd: BridgeCommand): Promise<Record<string, unknown>> {
    if (!this.wa) {
      return { type: 'error', error: 'WhatsApp client not initialized' };
    }

    switch (cmd.type) {
      case 'send':
        await this.wa.sendMessage(cmd.to, cmd.text);
        return { type: 'sent', to: cmd.to };

      case 'backfill': {
        // Fire-and-forget: history arrives later on 'messaging-history.set'
        // and is broadcast as ordinary messages flagged historical.
        const requestId = await this.wa.fetchHistory(cmd.jid, cmd.count ?? 50);
        return { type: 'backfill_requested', jid: cmd.jid, requestId };
      }

      case 'health':
        return { type: 'health', ...this.wa.getHealth() };

      case 'contacts':
        return { type: 'contacts', contacts: this.wa.findContacts(cmd.query || '') };

      default:
        return { type: 'error', error: `Unknown command: ${JSON.stringify(cmd)}` };
    }
  }

  private broadcast(msg: BridgeMessage): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(data);
      }
    }
  }

  async stop(): Promise<void> {
    // Close all client connections
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    // Close WebSocket server
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }

    // Disconnect WhatsApp
    if (this.wa) {
      await this.wa.disconnect();
      this.wa = null;
    }
  }
}
