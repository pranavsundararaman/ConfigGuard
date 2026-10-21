import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, AlertCircle, ArrowDownRight, ArrowUpRight, Check, CheckCircle2, ChevronRight, Clock3, Code2, FileClock, FilePlus2, FolderKanban, Gauge, History, Layers3, LoaderCircle, Play, RefreshCw, Search, Shield, ShieldAlert, ShieldCheck, TriangleAlert, X } from 'lucide-react'
import { api, normalizeHistoricalResults, normalizeLimitations, normalizeStringList, type AnalysisRecord, type ConfigFormat, type ConfigVersion, type HistoricalResult } from './api'
import './App.css'

type View = 'overview' | 'analyze' | 'configurations' | 'analyses'
const NAV: Array<{ id: View; title: string; icon: typeof Gauge }> = [
  { id: 'overview', title: 'Overview', icon: Gauge },
  { id: 'analyze', title: 'Analyze config', icon: Activity },
  { id: 'configurations', title: 'Configurations', icon: Layers3 },
  { id: 'analyses', title: 'Analyses', icon: History },
]
const JSON_EXAMPLE = '{\n  "service": {\n    "replicas": 3,\n    "timeout_seconds": 30\n  },\n  "database": {\n    "pool_size": 12\n  }\n}'

function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function asError(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please try again.' }
function isObject(value: unknown): value is Record<string, unknown> { return value !== null && typeof value === 'object' && !Array.isArray(value) }

function App() {
  const [view, setView] = useState<View>('analyze')
  const [projectId, setProjectId] = useState(() => localStorage.getItem('configguard-project') || 'configguard-demo')
  const [projectDraft, setProjectDraft] = useState(projectId)
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const [versions, setVersions] = useState<ConfigVersion[]>([])
  const [analyses, setAnalyses] = useState<AnalysisRecord[]>([])
  const [loading, setLoading] = useState(false)
  const [pageError, setPageError] = useState('')
  const [format, setFormat] = useState<ConfigFormat>('json')
  const [configText, setConfigText] = useState(JSON_EXAMPLE)
  const [minObservations, setMinObservations] = useState('3')
  const [editorError, setEditorError] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [latestAnalysis, setLatestAnalysis] = useState<AnalysisRecord | null>(null)
  const [savingVersion, setSavingVersion] = useState(false)
  const [deployingId, setDeployingId] = useState('')
  const [selectedAnalysis, setSelectedAnalysis] = useState<AnalysisRecord | null>(null)
  const [filter, setFilter] = useState('')

  const refresh = useCallback(async () => {
    if (!projectId.trim()) return
    setLoading(true); setPageError('')
    const results = await Promise.allSettled([api.versions(projectId), api.analyses(projectId)])
    if (results[0].status === 'fulfilled') setVersions(results[0].value)
    if (results[1].status === 'fulfilled') setAnalyses(results[1].value)
    const failure = results.find((result) => result.status === 'rejected')
    if (failure?.status === 'rejected') setPageError(asError(failure.reason))
    setLoading(false)
  }, [projectId])

  useEffect(() => {
    let active = true
    api.health().then(() => { if (active) setHealth('online') }).catch(() => { if (active) setHealth('offline') })
    return () => { active = false }
  }, [])
  // Project changes trigger a backend refresh, including the initial project load.
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => { void refresh() }, [refresh])

  const deployedCount = versions.filter((item) => item.lifecycle === 'deployed').length
  const proposalCount = versions.filter((item) => item.lifecycle === 'proposed').length
  const latestActivity = useMemo(() => [...versions.map((item) => item.created_at), ...analyses.map((item) => item.created_at)].filter(Boolean).sort().at(-1), [versions, analyses])

  function changeProject(event: React.FormEvent) {
    event.preventDefault()
    const next = projectDraft.trim()
    if (!next) return
    setProjectId(next); localStorage.setItem('configguard-project', next)
    setLatestAnalysis(null); setSelectedAnalysis(null); setPageError('')
  }

  function parseEditor(nextText = configText, nextFormat = format) {
    if (nextFormat !== 'json') {
      setEditorError(`${nextFormat.toUpperCase()} parsing is not available in this browser build. Choose JSON to analyze or save a configuration. No request was sent.`)
      return
    }
    try {
      const value: unknown = JSON.parse(nextText)
      if (!isObject(value)) throw new Error('The top level value must be a JSON object.')
      setEditorError('')
      return value
    } catch (error) {
      setEditorError(asError(error)); return null
    }
  }

  async function runAnalysis(event: React.FormEvent) {
    event.preventDefault()
    const object = parseEditor()
    const minimum = Number(minObservations)
    if (!object || !Number.isInteger(minimum) || minimum < 3 || minimum > 100) {
      if (object && (minimum < 3 || minimum > 100 || !Number.isInteger(minimum))) setEditorError('Minimum observations must be a whole number from 3 to 100.')
      return
    }
    setAnalyzing(true); setPageError('')
    try {
      const result = await api.analyze(projectId, object, format, minimum)
      setLatestAnalysis(result); setView('analyze'); await refresh()
    } catch (error) { setPageError(asError(error)) } finally { setAnalyzing(false) }
  }

  async function saveVersion() {
    const object = parseEditor()
    if (!object) return
    setSavingVersion(true); setPageError('')
    try { await api.saveVersion(projectId, object, format); await refresh(); setView('configurations') }
    catch (error) { setPageError(asError(error)) } finally { setSavingVersion(false) }
  }

  async function deployVersion(version: ConfigVersion) {
    if (!window.confirm(`Mark this configuration as deployed for project “${projectId}”? It will be eligible for future historical baselines.`)) return
    setDeployingId(version.id); setPageError('')
    try { await api.deployVersion(projectId, version.id); await refresh() }
    catch (error) { setPageError(asError(error)) } finally { setDeployingId('') }
  }

  async function openAnalysis(analysis: AnalysisRecord) {
    setSelectedAnalysis(analysis)
    try { setSelectedAnalysis(await api.analysis(projectId, analysis.id)) } catch (error) { setPageError(asError(error)) }
  }

  const filteredVersions = versions.filter((item) => `${item.id} ${item.format} ${item.lifecycle}`.toLowerCase().includes(filter.toLowerCase()))
  const title = NAV.find((item) => item.id === view)?.title ?? 'Overview'

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#overview" onClick={(event) => { event.preventDefault(); setView('overview') }}><span className="brand-mark"><Shield size={19} strokeWidth={2.4} /></span><span>config<span className="brand-light">guard</span><small>CONFIG RISK INTELLIGENCE</small></span></a>
      <div className="side-label">WORKSPACE</div>
      <form className="project-box" onSubmit={changeProject}>
        <label htmlFor="project-id">PROJECT</label>
        <div className="project-input-wrap"><FolderKanban size={15} /><input id="project-id" value={projectDraft} onChange={(event) => setProjectDraft(event.target.value)} aria-label="Project identifier" /></div>
        <button type="submit" className="change-project">Open project <ChevronRight size={14} /></button>
      </form>
      <div className="side-label nav-label">ANALYSIS</div>
      <nav className="main-nav" aria-label="Main navigation">
        {NAV.map(({ id, title: label, icon: Icon }) => <button key={id} className={`nav-item ${view === id ? 'active' : ''}`} onClick={() => { setView(id); setPageError('') }}><Icon size={17} /><span>{label}</span>{id === 'configurations' && versions.length > 0 && <small>{versions.length}</small>}</button>)}
      </nav>
      <div className="sidebar-bottom"><div className={`api-status ${health}`}><span className="status-dot" /><span>{health === 'checking' ? 'Checking API' : health === 'online' ? 'API reachable' : 'API unavailable'}</span><span className="api-service">{health === 'online' ? '●' : '○'}</span></div><div className="sidebar-foot">ConfigGuard V2.2 <span>·</span> Preview</div></div>
    </aside>

    <main className="main-panel">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><ChevronRight size={14} /><strong>{title}</strong></div><div className="top-actions"><span className="project-chip"><span className="mini-dot" />{projectId}</span><button className="icon-button" title="Refresh project data" onClick={() => void refresh()} disabled={loading}><RefreshCw size={16} className={loading ? 'spin' : ''} /></button><div className="avatar">CG</div></div></header>
      <div className="content">
        {pageError && <div className="alert error-alert" role="alert"><AlertCircle size={17} /><span>{pageError}</span><button onClick={() => setPageError('')} aria-label="Dismiss error"><X size={15} /></button></div>}
        {view === 'overview' && <Overview projectId={projectId} versions={versions} analyses={analyses} deployedCount={deployedCount} proposalCount={proposalCount} latestActivity={latestActivity} loading={loading} setView={setView} openAnalysis={openAnalysis} />}
        {view === 'analyze' && <section className="page-section">
          <PageHeading eyebrow="RISK WORKBENCH" title="Analyze a configuration" description="Compare proposed parameter values with deployed history for this project." />
          <div className="analysis-layout"><form className="panel editor-panel" onSubmit={runAnalysis}>
            <div className="panel-heading"><div><h2>Configuration input</h2><p>Paste the proposed configuration content.</p></div><span className="step-badge"><Code2 size={15} /> INPUT</span></div>
            <div className="editor-toolbar"><label htmlFor="format">FORMAT</label><select id="format" value={format} onChange={(event) => { const next = event.target.value as ConfigFormat; setFormat(next); setEditorError(next === 'json' ? '' : `${next.toUpperCase()} parsing is not available in this browser build. Choose JSON to analyze or save a configuration. No request was sent.`) }}><option value="json">JSON · supported</option><option value="yaml" disabled>YAML · unavailable</option><option value="yml" disabled>YML · unavailable</option><option value="env" disabled>.env · unavailable</option></select><span className="editor-hint">Object at root · UTF-8</span></div>
            <label className="sr-only" htmlFor="config-editor">Configuration content</label><textarea id="config-editor" className="code-editor" spellCheck={false} value={configText} onChange={(event) => { setConfigText(event.target.value); setEditorError('') }} aria-describedby="format-note" />
            <div id="format-note" className="format-note"><AlertCircle size={14} /> YAML and .env are parsed by the backend, but this UI currently sends structured JSON only.</div>
            <div className="editor-footer"><label className="minimum-setting">MIN. OBSERVATIONS <input type="number" min={3} max={100} step={1} value={minObservations} onChange={(event) => setMinObservations(event.target.value)} aria-label="Minimum historical observations" /></label><span className="muted tiny">At least 3 required by the API</span></div>
            {editorError && <div className="inline-error" role="alert"><AlertCircle size={15} />{editorError}</div>}
            <div className="form-actions"><button type="button" className="button secondary" onClick={() => void saveVersion()} disabled={savingVersion || format !== 'json'}>{savingVersion ? <LoaderCircle className="spin" size={16} /> : <FilePlus2 size={16} />} Save as proposal</button><button type="submit" className="button primary" disabled={analyzing || format !== 'json'}>{analyzing ? <LoaderCircle className="spin" size={16} /> : <Play size={15} fill="currentColor" />} Run historical analysis</button></div>
          </form>
            <aside className="panel guidance-panel"><div className="guidance-icon"><ShieldCheck size={20} /></div><h2>Historical analysis</h2><p>Compares numeric values against prior deployed versions in <strong>{projectId}</strong>.</p><div className="guidance-rule" /><div className="guidance-row"><span>Baseline source</span><strong>Deployed only</strong></div><div className="guidance-row"><span>Minimum sample</span><strong>{minObservations || 3} observations</strong></div><div className="guidance-row"><span>Proposal lifecycle</span><strong>Not auto-deployed</strong></div><div className="advisory-note"><TriangleAlert size={15} /><span>Unusual values suggest review. They do not predict deployment failure.</span></div></aside>
          </div>
          {latestAnalysis && <AnalysisResult analysis={latestAnalysis} onOpenHistory={() => setView('analyses')} />}
        </section>}
        {view === 'configurations' && <section className="page-section"><PageHeading eyebrow="PROJECT HISTORY" title="Configurations" description="Saved configuration versions and their deployment lifecycle." action={<button className="button primary" onClick={() => setView('analyze')}><FilePlus2 size={16} /> New configuration</button>} />
          <div className="toolbar-row"><div className="table-summary"><span className="summary-dot" />{versions.length} versions <span className="divider-dot">·</span> {deployedCount} deployed</div><div className="search-wrap"><Search size={15} /><input placeholder="Filter versions" value={filter} onChange={(event) => setFilter(event.target.value)} aria-label="Filter configuration versions" /></div></div>
          {filteredVersions.length ? <div className="table-wrap"><table><thead><tr><th>VERSION</th><th>FORMAT</th><th>LIFECYCLE</th><th>SAVED</th><th className="right">ACTION</th></tr></thead><tbody>{filteredVersions.map((version) => <tr key={version.id}><td><details className="version-details"><summary className="version-name"><span className="version-icon"><Code2 size={15} /></span><span><strong>{version.id.slice(-8)}</strong><small>{Object.keys(version.configuration || {}).length} root keys · inspect content</small></span></summary><pre>{JSON.stringify(version.configuration, null, 2)}</pre></details></td><td><span className="format-tag">{version.format.toUpperCase()}</span></td><td><LifecycleTag lifecycle={version.lifecycle} /></td><td className="date-cell">{formatDate(version.created_at)}</td><td className="right">{version.lifecycle === 'proposed' ? <button className="text-action" disabled={deployingId === version.id} onClick={() => void deployVersion(version)}>{deployingId === version.id ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />} Mark deployed</button> : <span className="muted tiny">Baseline eligible</span>}</td></tr>)}</tbody></table></div> : <EmptyState icon={<Layers3 size={21} />} title={loading ? 'Loading configurations…' : 'No configurations yet'} description={loading ? 'Fetching project history from the API.' : 'Save a configuration proposal from Analyze config to start project history.'} action={!loading && <button className="button secondary" onClick={() => setView('analyze')}>Add configuration</button>} />}
          <div className="lifecycle-note"><ShieldCheck size={17} /><span>Only versions explicitly marked <strong>deployed</strong> are included in historical baselines. New proposals stay out until accepted.</span></div>
        </section>}
        {view === 'analyses' && <section className="page-section"><PageHeading eyebrow="RISK HISTORY" title="Analyses" description="Review historical checks and their parameter-level findings." action={<button className="button primary" onClick={() => setView('analyze')}><Activity size={16} /> Run analysis</button>} />
          {analyses.length ? <div className="analysis-history">{analyses.map((analysis) => <button key={analysis.id} className={`history-row ${selectedAnalysis?.id === analysis.id ? 'selected' : ''}`} onClick={() => void openAnalysis(analysis)}><span className="history-symbol"><FileClock size={17} /></span><span className="history-main"><strong>{analysis.method || 'Historical analysis'}</strong><small>{normalizeHistoricalResults(analysis.results).length} parameters checked · {normalizeStringList(analysis.baseline_version_ids).length} deployed baseline versions</small></span><span className="history-date">{formatDate(analysis.created_at)}</span><ChevronRight size={16} className="history-chevron" /></button>)}</div> : <EmptyState icon={<History size={21} />} title={loading ? 'Loading analyses…' : 'No analyses yet'} description={loading ? 'Fetching saved project analyses.' : 'Run a historical analysis to create the first record.'} action={!loading && <button className="button secondary" onClick={() => setView('analyze')}>Analyze a configuration</button>} />}
          {selectedAnalysis && <AnalysisResult analysis={selectedAnalysis} onOpenHistory={() => setSelectedAnalysis(null)} />}
        </section>}
      </div>
    </main>
  </div>
}

function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}

function Overview({ projectId, versions, analyses, deployedCount, proposalCount, latestActivity, loading, setView, openAnalysis }: { projectId: string; versions: ConfigVersion[]; analyses: AnalysisRecord[]; deployedCount: number; proposalCount: number; latestActivity?: string; loading: boolean; setView: (view: View) => void; openAnalysis: (analysis: AnalysisRecord) => void }) {
  const recentAnalyses = analyses.slice(0, 4)
  const recentVersions = versions.slice(0, 4)
  return <section className="page-section"><div className="welcome-row"><div><div className="eyebrow">PROJECT DASHBOARD</div><h1>Project overview</h1><p>Configuration change intelligence for <strong>{projectId}</strong>.</p></div><button className="button primary" onClick={() => setView('analyze')}><Activity size={16} /> New analysis</button></div>
    <div className="summary-grid"><MetricCard label="Configuration versions" value={versions.length} detail={`${deployedCount} deployed · ${proposalCount} proposed`} icon={<Layers3 size={17} />} accent="blue" /><MetricCard label="Historical analyses" value={analyses.length} detail="Saved to project history" icon={<FileClock size={17} />} accent="purple" /><MetricCard label="Deployed baselines" value={deployedCount} detail="Eligible for comparison" icon={<ShieldCheck size={17} />} accent="green" /><MetricCard label="Latest activity" value={formatDate(latestActivity)} detail={latestActivity ? 'Most recent saved record' : 'No project activity yet'} icon={<Clock3 size={17} />} accent="amber" compact /> </div>
    <div className="overview-columns"><div className="panel overview-panel"><div className="panel-heading"><div><h2>Recent analyses</h2><p>Historical checks for this project</p></div><button className="quiet-action" onClick={() => setView('analyses')}>View all <ArrowUpRight size={14} /></button></div>{recentAnalyses.length ? <div className="compact-list">{recentAnalyses.map((analysis) => <button className="compact-row" key={analysis.id} onClick={() => { openAnalysis(analysis); setView('analyses') }}><span className="compact-icon"><Activity size={15} /></span><span className="compact-copy"><strong>{analysis.method || 'Historical analysis'}</strong><small>{normalizeHistoricalResults(analysis.results).length} parameters · {formatDate(analysis.created_at)}</small></span><ChevronRight size={15} /></button>)}</div> : <div className="small-empty">{loading ? 'Loading activity…' : 'No historical analyses have been saved.'}<button onClick={() => setView('analyze')}>Run first analysis <ArrowDownRight size={14} /></button></div>}</div>
      <div className="panel overview-panel"><div className="panel-heading"><div><h2>Configuration history</h2><p>Lifecycle across saved versions</p></div><button className="quiet-action" onClick={() => setView('configurations')}>View all <ArrowUpRight size={14} /></button></div>{recentVersions.length ? <div className="compact-list">{recentVersions.map((version) => <div className="compact-row static-row" key={version.id}><span className="compact-icon"><Code2 size={15} /></span><span className="compact-copy"><strong>Version {version.id.slice(-8)}</strong><small>{version.format.toUpperCase()} · {formatDate(version.created_at)}</small></span><LifecycleTag lifecycle={version.lifecycle} /></div>)}</div> : <div className="small-empty">{loading ? 'Loading history…' : 'No configurations have been saved.'}<button onClick={() => setView('analyze')}>Add configuration <ArrowDownRight size={14} /></button></div>}</div></div>
    <div className="overview-footnote"><Shield size={15} /><span>Historical findings are advisory. An unusual configuration value is not proof a deployment will fail.</span></div>
  </section>
}

function MetricCard({ label, value, detail, icon, accent, compact = false }: { label: string; value: string | number; detail: string; icon: React.ReactNode; accent: string; compact?: boolean }) {
  return <div className={`metric-card ${accent}`}><div className="metric-top"><span>{label}</span><i>{icon}</i></div><strong className={compact ? 'metric-date' : ''}>{value}</strong><small>{detail}</small></div>
}

function LifecycleTag({ lifecycle }: { lifecycle: string }) {
  return <span className={`lifecycle-tag ${lifecycle === 'deployed' ? 'deployed' : 'proposed'}`}>{lifecycle === 'deployed' ? <CheckCircle2 size={13} /> : <Clock3 size={13} />}{lifecycle}</span>
}

function EmptyState({ icon, title, description, action }: { icon: React.ReactNode; title: string; description: string; action?: React.ReactNode }) {
  return <div className="empty-state"><div className="empty-icon">{icon}</div><h2>{title}</h2><p>{description}</p>{action}</div>
}

function AnalysisResult({ analysis, onOpenHistory }: { analysis: AnalysisRecord; onOpenHistory: () => void }) {
  const results = normalizeHistoricalResults(analysis.results)
  const limitations = normalizeLimitations(analysis.limitations)
  const baselineVersionIds = normalizeStringList(analysis.baseline_version_ids)
  const proposedVersionId = typeof analysis.proposed_version_id === 'string' ? analysis.proposed_version_id : '—'
  const reviewRecommended = analysis.review_recommended === true
  const minimumObservations = typeof analysis.minimum_observations === 'number' && Number.isFinite(analysis.minimum_observations) ? analysis.minimum_observations : 3
  return <section className="panel result-panel"><div className="result-header"><div><div className="eyebrow">ANALYSIS RESULT <span className="result-id">· {analysis.id.slice(-8)}</span></div><h2>{reviewRecommended ? <><ShieldAlert size={20} /> Review recommended</> : <><ShieldCheck size={20} /> No unusual values found</>}</h2><p>{analysis.method || 'Historical median and MAD comparison'} · minimum {minimumObservations} observations</p></div><button className="icon-button" onClick={onOpenHistory} title="Close result"><X size={17} /></button></div>
    {limitations.length ? <div className="result-limitation"><TriangleAlert size={15} /><span>{limitations.join(' ')}</span></div> : <div className="result-limitation"><TriangleAlert size={15} /><span>This is a historical comparison, not a prediction of deployment success or failure.</span></div>}
    {results.length ? <div className="results-table-wrap"><table className="results-table"><thead><tr><th>PARAMETER</th><th>PROPOSED</th><th>OBSERVATIONS</th><th>BASELINE</th><th>ASSESSMENT</th></tr></thead><tbody>{results.map((item) => <tr key={item.parameter}><td className="parameter-name">{item.parameter}</td><td className="value-cell"><code>{String(item.proposed_value)}</code></td><td>{item.observation_count}{item.required_observations ? <span className="muted"> / {item.required_observations} min</span> : ''}</td><td className="baseline-cell">{item.baseline_median !== undefined ? <>Median <code>{item.baseline_median}</code><br />MAD <code>{item.baseline_mad ?? '—'}</code></> : <span className="muted">—</span>}</td><td><ResultStatus status={item.status} /><small className="explanation">{item.explanation}</small></td></tr>)}</tbody></table></div> : <div className="no-results">No numeric configuration parameters were available to assess.</div>}
    <div className="result-footer"><span><CheckCircle2 size={15} /> Analysis saved · {baselineVersionIds.length} deployed versions in baseline</span><button className="quiet-action" onClick={onOpenHistory}>Close details <ChevronRight size={14} /></button></div>
    <div className="analysis-identifiers"><span>ANALYSIS <code>{analysis.id}</code></span><span>PROPOSAL <code>{proposedVersionId}</code></span><span>BASELINE VERSIONS <code>{baselineVersionIds.join(', ') || 'None'}</code></span></div>
  </section>
}

function ResultStatus({ status }: { status: HistoricalResult['status'] }) {
  const labels = { not_assessed_non_numeric: 'Not assessed', insufficient_history: 'Insufficient history', unusual: 'Unusual', within_historical_range: 'Within range' }
  return <span className={`result-status ${status}`}>{status === 'unusual' ? <TriangleAlert size={13} /> : status === 'within_historical_range' ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />}{labels[status]}</span>
}

export default App
