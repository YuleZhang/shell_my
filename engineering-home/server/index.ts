import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { loadConfig, DATA_DIR } from './config.js';
import { Store } from './store.js';
import { buildApp } from './app.js';

process.umask(0o077);
async function main() {
  const config = loadConfig();
  const store = new Store(DATA_DIR);
  const root = fileURLToPath(new URL('../dist/web', import.meta.url));
  const app = await buildApp(config, store, { staticRoot: existsSync(root) ? root : undefined });
  let closing = false;
  const close = async () => { if (closing) return; closing = true; await app.close(); store.close(); };
  process.once('SIGINT', () => { void close(); });
  process.once('SIGTERM', () => { void close(); });
  try { await app.listen({ host: config.listenHost, port: config.port }); }
  catch { await close(); throw new Error('Server startup failed'); }
  console.info('Engineering home listening; integrations refresh only on explicit request');
}
main().catch(() => { console.error('Engineering home startup failed; verify private server configuration'); process.exitCode = 1; });
