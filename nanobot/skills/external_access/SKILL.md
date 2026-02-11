---
name: external_access
description: Access files outside the workspace (Windows/Linux/VPS).
metadata: {"nanobot":{"emoji":"📂"}}
---

# External Access

This skill enables the AI to access and manipulate files outside the current workspace directory using absolute paths. It is particularly useful when the bot needs to interact with system configuration files, logs, or other resources located in specific directories on the host machine (e.g., VPS).

## Instructions for External Access

### 1. Identify the Path
Determine the absolute path of the file or directory you need to access.

- **Windows:** Paths typically start with a drive letter, e.g., `C:\Users\Name\Documents\file.txt`.
- **Linux / VPS:** Paths use forward slashes and start from the root, e.g., `/etc/nginx/nginx.conf` or `/var/log/syslog`.

### 2. Verify Path Existence
Before attempting to read or modify a file, verify its existence and contents using `list_dir` or `find_by_name`. This avoids errors and ensures you are working with the correct file.

**Example (Windows):**
```
list_dir(DirectoryPath="C:\\Projects\\ExternalData")
```

**Example (Linux/VPS):**
```
list_dir(DirectoryPath="/var/www/html")
```

### 3. Accessing Files
Use the `view_file` tool with the *absolute path* to read file contents. Do not rely on relative paths when working outside the workspace.

**Example:**
```
view_file(AbsolutePath="/etc/hosts")
```

### 4. Modifying Files
When modifying external files, use `replace_file_content` or `write_to_file`, strictly adhering to the absolute path. Ensure you have the necessary permissions.

## Best Practices
- **Confirm Context:** Always double-check if the path is relevant to the current environment (e.g., local dev vs. remote VPS).
- **Use Absolute Paths:** Always use the full absolute path to avoid ambiguity.
- **Check Permissions:** Be aware that accessing system files might require elevated permissions, which the AI may not always have directly, depending on the environment setup.
