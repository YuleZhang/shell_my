import { ArrowRight, Blocks, FileText, GitPullRequest, KeyRound } from 'lucide-react';
import type { AccountsResponse, Capabilities, MrPage, ReportsResponse, Service, Snapshot } from '../../../shared/contracts';
import { useResource } from '../hooks';
import { Badge, dateTime, Empty, ExternalLink, Loading, Notice, PageHeading, Panel, SnapshotStatus } from '../presentation';
import { EndpointView } from './Services';

export function Overview({ capabilities }: { capabilities: Capabilities }) {
  const mrs = useResource<Snapshot<MrPage>>('/api/mrs?state=opened&page=1&perPage=20');
  const accounts = useResource<AccountsResponse>('/api/accounts');
  const services = useResource<Snapshot<Service[]>>('/api/services');
  const reports = useResource<ReportsResponse>('/api/reports');
  const values = accounts.data?.accounts ?? [];
  const serviceItems = services.data?.data ?? [];
  const accountAlerts = values.filter(a => !a.available || !!a.usage.error || a.usage.stale).length;
  const failedServices = serviceItems.filter(s => s.state.split('/')[0] === 'failed').length;
  const summary = [
    { label: '待关注 MR', value: mrs.data ? `${mrs.data.data.items.length}${mrs.data.data.hasMore ? '+' : ''}` : '—', note: '已缓存待合并 · 非远端实时总数', icon: GitPullRequest, to: 'mrs' },
    { label: '账户待关注', value: accounts.data ? String(accountAlerts) : '—', note: '凭据不可用 / 缓存过期 / 刷新失败', icon: KeyRound, to: 'accounts' },
    { label: '最近周报', value: reports.data?.reports[0]?.week ?? (reports.data ? '尚未采集' : '—'), note: reports.data?.reports[0]?.generated ? '已生成 · 可继续编辑' : '手动采集、审核后才生成', icon: FileText, to: 'reports' },
    { label: '服务异常', value: services.data ? String(failedServices) : '—', note: `${serviceItems.length} 个缓存条目 · 非健康探测`, icon: Blocks, to: 'services' },
  ];
  return <><PageHeading eyebrow="YOUR DAILY LAUNCHPAD" title="让工程日常，更有条理" description="只看需要关注的事。缓存先到，刷新与生成由你决定。" />
    <div className="overview-banner"><div><Badge tone="good">个人工程快捷台</Badge><h2>从这里，回到你的工作</h2><p>浏览合并请求、标记 API 账户、整理周报、找到本机服务。<br />不是聊天控制台，不接管你的 Agent 工作流。</p></div><a className="button button-primary" href="#reports">整理本周进展<ArrowRight size={16} aria-hidden="true" /></a></div>
    <div className="stat-grid">{summary.map(({ label, value, note, icon: Icon, to }) => <a className="stat-card" href={`#${to}`} key={to}><div className="stat-label">{label}<Icon size={18} aria-hidden="true" /></div><strong className={to === 'reports' ? 'stat-date' : ''}>{value}</strong><small>{note}</small></a>)}</div>
    <div className="overview-grid"><Panel title="最近待合并 MR" aside={<a className="text-link" href="#mrs">全部 MR <ArrowRight size={14} /></a>}>
      {mrs.error && <Notice tone="error">{mrs.error}</Notice>}{!capabilities.mrs && <Notice tone="warn">GitLab 未配置 · 仅显示缓存</Notice>}{mrs.loading && <Loading />}{mrs.data && <SnapshotStatus snapshot={mrs.data} />}
      {mrs.data?.data.items.slice(0, 5).map(mr => <div className="list-row" key={mr.id}><span className="list-icon"><GitPullRequest size={17} /></span><div><ExternalLink url={mr.url}>{mr.title}</ExternalLink><small>{mr.project} · !{mr.iid} · {dateTime(mr.updatedAt)}</small></div><Badge tone="warn">待合并</Badge></div>)}{!mrs.loading && !mrs.data?.data.items.length && <Empty title="尚无可用的待合并记录">到 MR 页面检查条件或显式刷新。</Empty>}
    </Panel><Panel title="API 账户" aside={<a className="text-link" href="#accounts">账户管理 <ArrowRight size={14} /></a>}>
      {accounts.error && <Notice tone="error">{accounts.error}</Notice>}{!capabilities.accounts && <Notice tone="warn">账户集成未配置</Notice>}{accounts.loading && <Loading />}
      {values.slice(0, 4).map(account => <div className="overview-account" key={account.id}><div className="bucket-heading"><strong>{account.alias || account.originalName}</strong><Badge tone={!account.available || account.usage.error ? 'bad' : account.usage.stale ? 'warn' : 'good'}>{!account.available ? '不可用' : account.usage.stale ? '待刷新' : '已缓存'}</Badge></div><small>原账号：{account.originalName}</small>{account.usage.data.map(b => <div className="compact-bucket" key={`${b.source}:${b.id}`}><span>{b.label} <small>{b.source}</small></span><span>剩余 {b.remaining === null ? '未提供' : b.remaining.toLocaleString('zh-CN')} {b.currency ?? '（币种未提供）'}</span></div>)}{!account.usage.data.length && <small>余额尚未提供</small>}<small>更新于 {dateTime(account.usage.updatedAt)}</small>{account.usage.error && <Notice tone="error">{account.usage.error.message}</Notice>}</div>)}{!accounts.loading && !values.length && <Empty title="尚无账户元数据">凭据通过仓库外本机配置导入。</Empty>}<p className="helper">按原始池独立展示，不合计 usage / budgetPool。</p>
    </Panel><Panel title="常用服务" aside={<a className="text-link" href="#services">服务目录 <ArrowRight size={14} /></a>}>
      {services.error && <Notice tone="error">{services.error}</Notice>}{!capabilities.services && <Notice tone="warn">服务发现未启用</Notice>}{services.loading && <Loading />}{services.data && <SnapshotStatus snapshot={services.data} />}
      {serviceItems.filter(s => s.favorite).slice(0, 4).map(service => <div className="overview-service" key={service.id}><div className="bucket-heading"><strong>{service.alias || service.name}</strong><Badge>{service.state}</Badge></div>{service.endpoints.map(e => <EndpointView key={`${e.address}:${e.port}`} endpoint={e} />)}{!service.endpoints.length && <small>后台服务，无网络端点</small>}</div>)}{!services.loading && !serviceItems.some(s => s.favorite) && <Empty title="收藏你的常用入口">在服务目录中配置协议、说明与收藏。</Empty>}
    </Panel><Panel title="最近周报" aside={<a className="text-link" href="#reports">周报工作台 <ArrowRight size={14} /></a>}>
      {reports.error && <Notice tone="error">{reports.error}</Notice>}{!capabilities.reports && <Notice tone="warn">周报采集尚未配置</Notice>}{reports.loading && <Loading />}
      {reports.data?.reports.slice(0, 5).map(report => <a href="#reports" className="list-row report-row" key={report.id}><span className="list-icon"><FileText size={18} /></span><div><strong>{report.week} 周报</strong><small>{report.projectIds.length} 个项目 · v{report.revision} · {dateTime(report.updatedAt)}</small></div><Badge tone={report.generated ? 'good' : 'muted'}>{report.generated ? '已生成' : '已采集'}</Badge></a>)}{!reports.loading && !reports.data?.reports.length && <Empty title="还没有周报">选择项目、同意扫描后采集；没有自动模型调用。</Empty>}<p className="helper">报告在服务器保存。这里不扫描会话，不生成、不发布飞书。</p>
    </Panel></div></>;
}
