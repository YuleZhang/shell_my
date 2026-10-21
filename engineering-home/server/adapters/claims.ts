import type { Report } from '../../shared/contracts.js';

export type ClaimSource = Pick<Report, 'fragments' | 'previousTodo' | 'evidence'>;
const pending = /\b(?:todo|pending|unverified|not measured|not verified|plan(?:ned)?|target|goal)\b|待测|待验证|未验证|未测|计划|目标/i;
const verified = /\b(?:verified|measured|benchmarked|validated|proven)\b|已验证|实测|已测|验证通过/i;
const performance = /\b(?:performance|latency|throughput|faster|speedup|improv(?:ed|ement)|reduc(?:ed|tion)|optimized)\b|性能|延迟|吞吐|加速|提升|降低|优化/i;
const metrics: [string, RegExp][] = [
  ['latency', /latency|duration|time|延迟|耗时/gi], ['throughput', /throughput|fps|吞吐|帧率/gi],
  ['memory', /memory|ram|内存/gi], ['size', /size|payload|大小|体积/gi],
  ['accuracy', /accuracy|precision|psnr|iou|cosine|精度|准确率|余弦/gi],
  ['tests', /tests?|passed|测试|通过/gi], ['performance', /performance|speedup|性能|加速/gi],
];
function normalize(text: string): string {
  return text.normalize('NFKC').toLowerCase().replace(/\b\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:t[\d:.+z-]+)?\b/g, '')
    .replace(/\b(?=[a-f0-9]{7,64}\b)(?=[a-f0-9]*[a-f])[a-f0-9]+\b/g, '').replace(/^\s*(?:[-*]\s*)?\d+[.)、]\s*/gm, '')
    .replace(/(?<=\d)[,_](?=\d{3}(?:\D|$))/g, '').replace(/\bpercent\b|百分之/g, '%');
}
function metricAt(text: string, index: number): string {
  let best = 'number', distance = Infinity;
  for (const [name, pattern] of metrics) for (const m of text.matchAll(pattern)) {
    const d = m.index <= index ? index - m.index : m.index - index + 20;
    if (d < distance) { distance = d; best = name; }
  }
  return best;
}
function tokens(text: string): string[] {
  const values: string[] = [];
  const pattern = /(?<![\w.])[+-]?\d+(?:\.\d+)?\s*(%|milliseconds?|microseconds?|seconds?|ms|us|µs|μs|ns|s|gib|mib|kib|gb|mb|kb|bytes?|fps|hz|x|倍|毫秒|微秒|秒|个|次)?(?![\w])/g;
  for (const m of text.matchAll(pattern)) {
    let value = Number.parseFloat(m[0]), unit = m[1] ?? '';
    const metric = metricAt(text, m.index);
    if (!unit && metric === 'number' && !/\b(?:passed|completed|reduced|improved)\b|通过|完成|降低|提升/.test(text)) continue;
    if (/^(?:ms|milliseconds?|毫秒)$/.test(unit)) { unit = 'time'; value *= 1000; }
    else if (/^(?:us|µs|μs|microseconds?|微秒)$/.test(unit)) unit = 'time';
    else if (unit === 'ns') { unit = 'time'; value /= 1000; }
    else if (/^(?:s|seconds?|秒)$/.test(unit)) { unit = 'time'; value *= 1000000; }
    else if (/^[gmk](?:i)?b$/.test(unit)) { value *= 1024 ** ({ k: 1, m: 2, g: 3 }[unit[0]]!); unit = 'bytes'; }
    else if (/^bytes?$/.test(unit)) unit = 'bytes';
    values.push(`${metric}:${Number(value.toPrecision(12))}:${unit}`);
  }
  return values;
}
export function claimWarnings(text: string, source?: ClaimSource): string[] {
  const sources = source ? [source.fragments, source.previousTodo, ...source.evidence.commits.flatMap(c =>
    [c.title, c.body, c.context ?? '', ...c.mrs.map(m => m.title)])] : [];
  const lines = sources.flatMap(s => normalize(s).split(/\n|[;；]/)).filter(s => !pending.test(s));
  const supported = new Set(lines.flatMap(tokens));
  let numeric = false, verification = false, qualitative = false;
  for (const line of normalize(text).split(/\n|[;；]/)) {
    if (pending.test(line)) continue;
    const values = tokens(line);
    if (values.some(v => !supported.has(v))) numeric = true;
    if (verified.test(line) && performance.test(line)) {
      const metric = metricAt(line, line.length);
      if (!lines.some(s => verified.test(s) && performance.test(s) && metricAt(s, s.length) === metric && values.every(v => tokens(s).includes(v)))) verification = true;
    }
    if (!values.length && performance.test(line) && /faster|speedup|improved|reduced|optimized|加速|提升|降低|优化/.test(line)) {
      if (!lines.some(s => s.trim() === line.trim().replace(/^[-*]\s*/, ''))) qualitative = true;
    }
  }
  return [numeric ? 'WARN: Unsupported numerical claim; selected evidence/fragments/Todo do not substantiate it. Treat as unverified.' : '',
    verification ? 'WARN: Unsupported verified performance claim; verification is not established by selected evidence. Treat as unverified.' : '',
    qualitative ? 'WARN: Unsupported performance improvement claim; treat as unverified until measured.' : ''].filter(Boolean);
}
