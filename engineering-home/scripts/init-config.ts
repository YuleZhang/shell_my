import { randomBytes } from 'node:crypto';
import { existsSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { hashPassword } from '../server/security/auth.js';
import { assertNoSymlinks, privateDirectory } from '../server/security/files.js';
import { CONFIG_DIR, validateConfig } from '../server/config.js';

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  input += chunk;
  if (input.length > 1024) { console.error('Password input too large'); process.exit(1); }
});
process.stdin.on('end', () => {
  try {
    const password = input.replace(/\r?\n$/, '');
    if (password.length < 12 || password.length > 512) throw new Error('Password must contain 12–512 characters');
    const config = validateConfig({ publicOrigin: 'http://localhost:4310', port: 4310,
      auth: { passwordHash: hashPassword(password), cookieSecret: randomBytes(32).toString('hex'), secureCookie: false },
      projectRoots: [{ id: 'shell-my', label: 'shell_my', root: fileURLToPath(new URL('../../', import.meta.url)) }],
      services: { enabled: true },
    });
    privateDirectory(CONFIG_DIR);
    const file = join(CONFIG_DIR, 'config.json'); assertNoSymlinks(file);
    if (existsSync(file)) throw new Error('Configuration exists; refusing to overwrite');
    writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
    console.info('Private config created. Edit project roots/integrations locally before starting. No provider credentials imported.');
  } catch (error) {
    console.error(error instanceof Error ? error.message : 'Configuration creation failed'); process.exitCode = 1;
  }
});
