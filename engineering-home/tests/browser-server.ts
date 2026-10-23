import { fileURLToPath } from 'node:url';
import { buildApp } from '../server/app.js';
import { sandbox, DUMMY_KEY, emptyEvidence, jsonResponse } from './helpers.js';

// This server is exclusively a browser fixture: provider, credentials and collectors are synthetic.
const box = sandbox();
const uid = process.getuid!();
box.config.publicOrigin = 'http://127.0.0.1:4319';
box.config.port = 4319;
box.config.services.accessAddress = '192.0.2.10';
const app = await buildApp(box.config, box.store, {
  staticRoot: fileURLToPath(new URL('../dist/web', import.meta.url)),
  readKeys: () => `demo ${DUMMY_KEY}`,
  fetcher: async value => {
    const url = new URL(String(value));
    if (url.hostname === 'usage.example.test') return jsonResponse({ code: 200, data: {
      usages: [{ channel_name: '推广预算', used: 2, total: 100, currency: 'CNY', reset_cycle: 'one-time' }],
      budget_pool_usages: [{ channel_name: '独立共享池', used: 10, total: 50, currency: 'CNY' }],
    } });
    if (url.hostname === 'gitlab.example.test' && url.pathname.endsWith('/user')) return jsonResponse({ id: 7, bot: false });
    if (url.hostname === 'gitlab.example.test') return jsonResponse([{ id: 1, iid: 3, project_id: 10,
      title: 'Fixture: 工程快捷台 MR', state: url.searchParams.get('state') === 'merged' ? 'merged' : 'opened',
      author: { id: 7 }, source_branch: 'feature', target_branch: 'main', updated_at: '2026-10-21T00:00:00Z',
      web_url: 'https://gitlab.example.test/demo/-/merge_requests/3', references: { full: 'team/demo!3' },
      reviewers: [{ name: 'Fixture Reviewer' }], head_pipeline: { status: 'success' },
    }], { 'x-next-page': '' });
    if (url.hostname === 'model.example.test') return jsonResponse({ choices: [{ message: {
      content: '2026/10/19 -> 2026/10/25\nSituation\n- 示例工程\nAction\n- 示例工程\n  - 接口实现，性能待测\nOthers:\n- 无\nResult\n- 示例工程\n  - 保留待验证状态\nTodo\n- 继续验证',
    } }] });
    throw new Error('Unexpected fixture provider');
  },
  collectEvidence: async () => ({ ...emptyEvidence, commits: [{ projectId: 'demo', sha: 'a'.repeat(40),
    time: '2026-10-21T00:00:00Z', title: '接口实现，性能待测', body: '', branch: 'feature', context: null,
    match: 'sha', mrs: [], mrStatus: 'unverified' }] }),
  run: async (command, args) => {
    if (command === '/usr/bin/systemctl') {
      if (args.includes('list-units')) return 'demo.service loaded active running Fixture web\nidle.service loaded inactive dead Fixture worker';
      if (args.includes('list-unit-files')) return 'demo.service enabled\nidle.service disabled';
      return 'Id=demo.service\nDescription=Fixture web\nActiveState=active\nSubState=running\nMainPID=42\nControlGroup=/fixture/demo.service\n\nId=idle.service\nDescription=Fixture worker\nActiveState=inactive\nSubState=dead\nMainPID=0\nControlGroup=\n';
    }
    if (command === '/usr/bin/ss') return args.includes('-4') ? 'LISTEN 0 128 0.0.0.0:8090 0.0.0.0:* users:(("node",pid=42,fd=2))' : '';
    throw new Error('Fixture collector unavailable');
  },
  procRead: path => path.endsWith('/status') ? `Uid:\t${uid}\t${uid}\t${uid}\t${uid}` : '0::/fixture/demo.service',
});
let closing = false;
async function close() { if (closing) return; closing = true; await app.close(); box.cleanup(); }
process.once('SIGINT', () => { void close(); });
process.once('SIGTERM', () => { void close(); });
await app.listen({ host: '127.0.0.1', port: 4319 });
console.info('Fixture-only browser backend ready at http://127.0.0.1:4319; no live integrations');
