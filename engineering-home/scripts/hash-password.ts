import { hashPassword } from '../server/security/auth.js';

// Accept dummy/new password only on stdin; never put it in argv or shell history.
let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { input += chunk; if (input.length > 1024) process.exit(1); });
process.stdin.on('end', () => {
  const password = input.replace(/\r?\n$/, '');
  if (password.length < 12 || password.length > 512) { console.error('Password must contain 12–512 characters'); process.exitCode = 1; return; }
  console.log(hashPassword(password));
});
