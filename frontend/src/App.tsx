import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Activity, AlertCircle, ArrowDownRight, ArrowUpRight, Check, CheckCircle2, ChevronRight, Clock3, Code2, FileClock, FilePlus2, FolderKanban, Gauge, GitPullRequest, History, Layers3, LoaderCircle, Play, RefreshCw, Search, Shield, ShieldAlert, ShieldCheck, TriangleAlert, X } from 'lucide-react'
import { api, getCIHistoryDisplayState, normalizeHistoricalResults, normalizeLimitations, normalizeStringList, type AnalysisRecord, type AuthSession, type CIAnalysisRecord, type ConfigFormat, type ConfigVersion, type GitHubRepository, type GitHubUser, type OwnedProject, type HistoricalResult } from './api'
import { defaultConfigurationExample, planFormatSwitch, resolveFormatSwitch, type FormatSwitchChoice, type FormatSwitchPlan, parseConfiguration } from './configParser'
import './App.css'

type View = 'overview' | 'analyze' | 'configurations' | 'analyses' | 'ci-analyses'
const NAV: Array<{ id: View; title: string; icon: typeof Gauge }> = [
  { id: 'overview', title: 'Overview', icon: Gauge },
  { id: 'analyze', title: 'Analyze config', icon: Activity },
  { id: 'configurations', title: 'Configurations', icon: Layers3 },
  { id: 'analyses', title: 'Analyses', icon: History },
  { id: 'ci-analyses', title: 'GitHub PR checks', icon: GitPullRequest },
]
function formatDate(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '—' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function asError(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please try again.' }
function App() {
  const [view, setView] = useState<View>('analyze')
  const [projectId, setProjectId] = useState('')
  const [projects, setProjects] = useState<OwnedProject[]>([])
  const [repositories, setRepositories] = useState<GitHubRepository[]>([])
  const [repositoryToAdd, setRepositoryToAdd] = useState('')
  const [authLoading, setAuthLoading] = useState(true)
  const [currentUser, setCurrentUser] = useState<GitHubUser | null>(null)
  const [, setCsrfToken] = useState<string | null>(null)
  const [health, setHealth] = useState<'checking' | 'online' | 'offline'>('checking')
  const [versions, setVersions] = useState<ConfigVersion[]>([])
  const [analyses, setAnalyses] = useState<AnalysisRecord[]>([])
  const [ciAnalyses, setCiAnalyses] = useState<CIAnalysisRecord[]>([])
  const [selectedCiAnalysis, setSelectedCiAnalysis] = useState<CIAnalysisRecord | null>(null)
  const [ciHistoryStatus, setCiHistoryStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [loading, setLoading] = useState(false)
  const [pageError, setPageError] = useState('')
  const [format, setFormat] = useState<ConfigFormat>('json')
  const [configText, setConfigText] = useState(() => defaultConfigurationExample('json'))
  const [formatSwitching, setFormatSwitching] = useState(false)
  const [formatSwitchPlan, setFormatSwitchPlan] = useState<FormatSwitchPlan | null>(null)
  const formatSwitchLock = useRef(false)
  const formatSelectRef = useRef<HTMLSelectElement>(null)
  const formatDialogRef = useRef<HTMLDivElement>(null)
  const wasFormatDialogOpen = useRef(false)
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
    setCiHistoryStatus('loading')
    const [versionsResult, analysesResult, ciResult] = await Promise.allSettled([
      api.versions(projectId),
      api.analyses(projectId),
      api.ciAnalyses(projectId),
    ])
    if (versionsResult.status === 'fulfilled') setVersions(versionsResult.value)
    if (analysesResult.status === 'fulfilled') setAnalyses(analysesResult.value)
    if (ciResult.status === 'fulfilled') {
      setCiAnalyses(ciResult.value); setCiHistoryStatus('ready')
    } else {
      setCiAnalyses([]); setCiHistoryStatus('error')
    }
    const failure = [versionsResult, analysesResult, ciResult].find((result) => result.status === 'rejected')
    if (failure?.status === 'rejected') setPageError(asError(failure.reason))
    setLoading(false)
  }, [projectId])

  useEffect(() => {
    let active = true
    api.health().then(() => { if (active) setHealth('online') }).catch(() => { if (active) setHealth('offline') })
    return () => { active = false }
  }, [])
  useEffect(() => {
    let active = true
    api.authMe().then(async (session: AuthSession) => {
      if (!active) return
      if (!session.authenticated || !session.user) { setCurrentUser(null); return }
      setCurrentUser(session.user)
      setCsrfToken(session.csrf_token)
      api.setCsrfToken(session.csrf_token)
      const [owned, available] = await Promise.all([api.projects(), api.repositories()])
      if (!active) return
      setProjects(owned); setRepositories(available)
      const selected = owned.find((project) => project.id === session.active_project_id) ?? owned[0]
      if (selected) {
        setProjectId(selected.id)
        if (selected.id !== session.active_project_id) await api.activateProject(selected.id)
      }
    }).catch((error) => { if (active) setPageError(asError(error)) }).finally(() => { if (active) setAuthLoading(false) })
    return () => { active = false }
  }, [])
  // Project changes trigger a backend refresh, including the initial project load.
  // oxlint-disable-next-line react/set-state-in-effect
  useEffect(() => { void refresh() }, [refresh])

  const deployedCount = versions.filter((item) => item.lifecycle === 'deployed').length
  const proposalCount = versions.filter((item) => item.lifecycle === 'proposed').length
  const latestActivity = useMemo(() => [...versions.map((item) => item.created_at), ...analyses.map((item) => item.created_at)].filter(Boolean).sort().at(-1), [versions, analyses])

  async function changeProject(next: string) {
    if (!next || next === projectId) return
    setPageError('')
    try {
      await api.activateProject(next)
      setProjectId(next)
      setLatestAnalysis(null); setSelectedAnalysis(null); setSelectedCiAnalysis(null); setCiAnalyses([])
    } catch (error) { setPageError(asError(error)) }
  }

  async function addRepository(repositoryId = repositoryToAdd) {
    const repository = repositories.find((item) => String(item.id) === repositoryId)
    if (!repository) return
    setPageError('')
    try {
      const project = await api.selectProject(repository.id)
      const owned = await api.projects()
      setProjects(owned); setProjectId(project.id); setRepositoryToAdd('')
    } catch (error) { setPageError(asError(error)) }
  }

  async function signOut() {
    try { await api.logout() } catch { /* Clear local state even if the session expired. */ }
    api.setCsrfToken(null); setCsrfToken(null); setCurrentUser(null); setProjects([]); setProjectId('')
  }

  async function parseEditor(nextText = configText, nextFormat = format) {
    try {
      const value = await parseConfiguration(nextText, nextFormat)
      setEditorError('')
      return value
    } catch (error) {
      setEditorError(asError(error)); return null
    }
  }

  useEffect(() => {
    if (formatSwitchPlan) {
      wasFormatDialogOpen.current = true
      formatDialogRef.current?.querySelector<HTMLButtonElement>('[data-autofocus="true"]')?.focus()
    } else if (wasFormatDialogOpen.current) {
      wasFormatDialogOpen.current = false
      formatSelectRef.current?.focus()
    }
  }, [formatSwitchPlan])

  async function switchFormat(nextFormat: ConfigFormat) {
    if (nextFormat === format || formatSwitchLock.current) return
    formatSwitchLock.current = true
    setFormatSwitching(true)
    setEditorError('')
    try {
      const plan = await planFormatSwitch(configText, format, nextFormat)
      if (plan.requiresChoice) setFormatSwitchPlan(plan)
      else applyFormatSwitch(plan, 'convert')
    } finally {
      formatSwitchLock.current = false
      setFormatSwitching(false)
    }
  }

  function applyFormatSwitch(plan: FormatSwitchPlan, choice: FormatSwitchChoice) {
    try {
      const result = resolveFormatSwitch(plan, choice)
      setFormat(result.format)
      setConfigText(result.content)
      setEditorError(choice === 'example' ? `Loaded a valid ${plan.targetFormat.toUpperCase()} example.` : '')
      setFormatSwitchPlan(null)
    } catch (error) {
      setEditorError(asError(error))
    }
  }

  function handleFormatDialogKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      if (formatSwitchPlan) applyFormatSwitch(formatSwitchPlan, 'cancel')
      return
    }
    if (event.key !== 'Tab' || !formatDialogRef.current) return
    const focusable = [...formatDialogRef.current.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')]
    if (!focusable.length) return
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus() }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
  }

  async function runAnalysis(event: React.FormEvent) {
    event.preventDefault()
    if (!projectId) { setPageError('Select a repository project before running analysis.'); return }
    const object = await parseEditor()
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
    if (!projectId) { setPageError('Select a repository project before saving a configuration.'); return }
    const object = await parseEditor()
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

  async function openCiAnalysis(analysis: CIAnalysisRecord) {
    setSelectedCiAnalysis(analysis)
    try { setSelectedCiAnalysis(await api.ciAnalysis(projectId, analysis.id)) }
    catch (error) { setPageError(asError(error)) }
  }

  const filteredVersions = versions.filter((item) => `${item.id} ${item.format} ${item.lifecycle}`.toLowerCase().includes(filter.toLowerCase()))
  const ciDisplayState = getCIHistoryDisplayState(Boolean(currentUser), ciHistoryStatus === 'loading' || loading, ciAnalyses.length > 0, ciHistoryStatus === 'error')
  const title = NAV.find((item) => item.id === view)?.title ?? 'Overview'

  if (authLoading) return <div className="auth-gate"><div className="panel"><LoaderCircle className="spin" size={20} /><p>Checking your ConfigGuard session…</p></div></div>
  if (!currentUser) return <div className="auth-gate"><div className="panel auth-panel"><span className="brand-mark"><Shield size={20} /></span><div className="eyebrow">CONFIGGUARD WORKSPACE</div><h1>Sign in with GitHub</h1><p>Connect your account to select public repositories and view project history securely.</p><a className="button primary" href="/api/auth/github/login">Continue with GitHub</a><small>ConfigGuard requests read-only profile access and lists public repositories only.</small></div></div>

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#overview" onClick={(event) => { event.preventDefault(); setView('overview') }}><span className="brand-mark"><Shield size={19} strokeWidth={2.4} /></span><span>config<span className="brand-light">guard</span><small>CONFIG RISK INTELLIGENCE</small></span></a>
      <div className="side-label">WORKSPACE</div>
      <div className="project-box">
        <label htmlFor="project-id">SELECTED PROJECT</label>
        <div className="project-input-wrap"><FolderKanban size={15} /><select id="project-id" value={projectId} onChange={(event) => void changeProject(event.target.value)} aria-label="Selected project">
          <option value="">Choose a repository project</option>{projects.map((project) => <option key={project.id} value={project.id}>{project.repository}</option>)}
        </select></div>
        {repositories.length > 0 && <><label htmlFor="repository-to-add" className="repo-add-label">ADD PUBLIC REPOSITORY</label><select id="repository-to-add" value={repositoryToAdd} onChange={(event) => setRepositoryToAdd(event.target.value)}><option value="">Choose repository…</option>{repositories.map((repository) => <option key={repository.id} value={repository.id}>{repository.full_name}</option>)}</select><button type="button" className="change-project" disabled={!repositoryToAdd} onClick={() => void addRepository()}>Add repository <ChevronRight size={14} /></button></>}
        <button type="button" className="change-project" onClick={() => void signOut()}>Sign out @{currentUser.login}</button>
      </div>
      <div className="side-label nav-label">ANALYSIS</div>
      <nav className="main-nav" aria-label="Main navigation">
        {NAV.map(({ id, title: label, icon: Icon }) => <button key={id} className={`nav-item ${view === id ? 'active' : ''}`} onClick={() => { setView(id); setPageError('') }}><Icon size={17} /><span>{label}</span>{id === 'configurations' && versions.length > 0 && <small>{versions.length}</small>}</button>)}
      </nav>
      <div className="sidebar-bottom"><div className={`api-status ${health}`}><span className="status-dot" /><span>{health === 'checking' ? 'Checking API' : health === 'online' ? 'API reachable' : 'API unavailable'}</span><span className="api-service">{health === 'online' ? '●' : '○'}</span></div><div className="sidebar-foot">ConfigGuard V2.5 <span>·</span> Preview</div></div>
    </aside>

    <main className="main-panel">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><ChevronRight size={14} /><strong>{title}</strong></div><div className="top-actions"><span className="project-chip"><span className="mini-dot" />{projects.find((item) => item.id === projectId)?.repository || 'No project selected'}</span><select className="mobile-project-select" value={projectId ? `project:${projectId}` : ''} aria-label="Switch or add repository" onChange={(event) => { const [kind, id] = event.target.value.split(':'); if (kind === 'project') void changeProject(id); else if (kind === 'repo') void addRepository(id) }}><option value="">Select repository</option><optgroup label="Your projects">{projects.map((project) => <option key={project.id} value={`project:${project.id}`}>{project.repository}</option>)}</optgroup><optgroup label="Add public repository">{repositories.filter((repository) => !projects.some((project) => project.github_repository_id === repository.id)).map((repository) => <option key={repository.id} value={`repo:${repository.id}`}>{repository.full_name}</option>)}</optgroup></select><button className="icon-button" title="Refresh project data" onClick={() => void refresh()} disabled={loading || !projectId}><RefreshCw size={16} className={loading ? 'spin' : ''} /></button><div className="avatar">{currentUser.login.slice(0, 2).toUpperCase()}</div></div></header>
      <div className="content">
        {pageError && <div className="alert error-alert" role="alert"><AlertCircle size={17} /><span>{pageError}</span><button onClick={() => setPageError('')} aria-label="Dismiss error"><X size={15} /></button></div>}
        {view === 'overview' && <Overview projectId={projectId} versions={versions} analyses={analyses} deployedCount={deployedCount} proposalCount={proposalCount} latestActivity={latestActivity} loading={loading} setView={setView} openAnalysis={openAnalysis} />}
        {view === 'analyze' && <section className="page-section">
          <PageHeading eyebrow="RISK WORKBENCH" title="Analyze a configuration" description="Compare proposed parameter values with deployed history for this project." />
          <div className="analysis-layout"><form className="panel editor-panel" onSubmit={runAnalysis}>
            <div className="panel-heading"><div><h2>Configuration input</h2><p>Paste the proposed configuration content.</p></div><span className="step-badge"><Code2 size={15} /> INPUT</span></div>
            <div className="editor-toolbar"><label htmlFor="format">FORMAT</label><select ref={formatSelectRef} id="format" value={format} disabled={formatSwitching || Boolean(formatSwitchPlan)} onChange={(event) => { void switchFormat(event.target.value as ConfigFormat) }}><option value="json">JSON</option><option value="yaml">YAML</option><option value="yml">YML</option><option value="env">ENV</option></select><span className="editor-hint">UTF-8 · object/mapping at root</span></div>
            <label className="sr-only" htmlFor="config-editor">Configuration content</label><textarea id="config-editor" className="code-editor" spellCheck={false} value={configText} disabled={formatSwitching || Boolean(formatSwitchPlan)} onChange={(event) => { setConfigText(event.target.value); setEditorError('') }} aria-describedby="format-note" />
            <div id="format-note" className="format-note"><AlertCircle size={14} /> ENV uses underscore-separated paths; underscores are path separators by convention. ENV values are text and numeric/boolean values are normalized by the backend parser. Only numeric values are assessed by historical analysis.</div>
            <div className="editor-footer"><label className="minimum-setting">MIN. OBSERVATIONS <input type="number" min={3} max={100} step={1} value={minObservations} onChange={(event) => setMinObservations(event.target.value)} aria-label="Minimum historical observations" /></label><span className="muted tiny">At least 3 required by the API</span></div>
            {editorError && <div className="inline-error" role="alert"><AlertCircle size={15} />{editorError}</div>}
            <div className="form-actions"><button type="button" className="button secondary" onClick={() => void saveVersion()} disabled={!projectId || savingVersion || formatSwitching || Boolean(formatSwitchPlan)}>{savingVersion ? <LoaderCircle className="spin" size={16} /> : <FilePlus2 size={16} />} Save as proposal</button><button type="submit" className="button primary" disabled={!projectId || analyzing || formatSwitching || Boolean(formatSwitchPlan)}>{analyzing ? <LoaderCircle className="spin" size={16} /> : <Play size={15} fill="currentColor" />} Run historical analysis</button></div>
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
        {view === 'ci-analyses' && <section className="page-section"><PageHeading eyebrow="GITHUB ACTIONS" title="Pull request analyses" description="Review sanitised, advisory CI reports associated with this project." />
          {!projectId ? <EmptyState icon={<GitPullRequest size={21} />} title="Select a repository" description="Choose one of your selected repository projects to view its authenticated pull request history." /> : ciDisplayState === 'error' ? <EmptyState icon={<AlertCircle size={21} />} title="Could not load CI history" description="Refresh your session and confirm the backend CI integration is available." /> : ciDisplayState === 'ready' ? <div className="analysis-history">{ciAnalyses.map((analysis) => <button key={analysis.id} className={`history-row ${selectedCiAnalysis?.id === analysis.id ? 'selected' : ''}`} onClick={() => void openCiAnalysis(analysis)}><span className="history-symbol"><GitPullRequest size={17} /></span><span className="history-main"><strong>{analysis.repository} #{analysis.pull_request_number} · {analysis.classification.replaceAll('_', ' ')}</strong><small>Score {analysis.risk_score.toFixed(2)} · {analysis.change_counts.configuration_changes} changes · {analysis.findings.length} findings</small></span><span className="history-date">{formatDate(analysis.created_at)}</span><ChevronRight size={16} className="history-chevron" /></button>)}</div> : <EmptyState icon={<GitPullRequest size={21} />} title={ciDisplayState === 'loading' ? 'Loading pull request history…' : 'No CI analyses yet'} description={ciDisplayState === 'loading' ? 'Fetching authenticated project reports.' : 'No GitHub Actions reports have been ingested for this project.'} />}
          {selectedCiAnalysis && <CIAnalysisDetail analysis={selectedCiAnalysis} />}
        </section>}
      </div>
    </main>
    {formatSwitchPlan && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) applyFormatSwitch(formatSwitchPlan, 'cancel') }}>
      <div ref={formatDialogRef} className="format-dialog" role="dialog" aria-modal="true" aria-labelledby="format-dialog-title" aria-describedby="format-dialog-description" onKeyDown={handleFormatDialogKeyDown}>
        <div className="format-dialog-icon"><Code2 size={18} /></div>
        <h2 id="format-dialog-title">Switch to {formatSwitchPlan.targetFormat.toUpperCase()}?</h2>
        <p id="format-dialog-description">{formatSwitchPlan.sourceFormat === 'env'
          ? 'ENV variable names are flat strings. Splitting underscores into nested path components is a deterministic convention, but it cannot reveal whether an underscore was part of a literal key. The converted hierarchy is inferred, not the original structure.'
          : 'Choose how to handle the current editor content.'}</p>
        {formatSwitchPlan.conversionError && <div className="format-dialog-error" role="status"><AlertCircle size={15} />{formatSwitchPlan.conversionError}</div>}
        <div className="format-dialog-actions">
          <button className="button primary" data-autofocus={formatSwitchPlan.convertedContent ? 'true' : undefined} disabled={!formatSwitchPlan.convertedContent} onClick={() => applyFormatSwitch(formatSwitchPlan, 'convert')}>Convert using convention</button>
          <button className="button secondary" data-autofocus={!formatSwitchPlan.convertedContent ? 'true' : undefined} onClick={() => applyFormatSwitch(formatSwitchPlan, 'example')}>Use format example</button>
          <button className="button quiet" onClick={() => applyFormatSwitch(formatSwitchPlan, 'cancel')}>Cancel</button>
        </div>
      </div>
    </div>}
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

function CIAnalysisDetail({ analysis }: { analysis: CIAnalysisRecord }) {
  const counts = analysis.change_counts
  return <section className="panel ci-detail-panel">
    <div className="ci-detail-header"><div><div className="eyebrow">PULL REQUEST #{analysis.pull_request_number}</div><h2>{analysis.repository}</h2><p>{analysis.classification.replaceAll('_', ' ')} · score {analysis.risk_score.toFixed(2)}</p></div><a className="button secondary" href={analysis.pull_request_url} target="_blank" rel="noreferrer noopener">Open pull request <ArrowUpRight size={14} /></a></div>
    <dl className="ci-commit-grid"><div><dt>Base commit</dt><dd>{analysis.base_sha.slice(0, 12)}</dd></div><div><dt>Head commit</dt><dd>{analysis.head_sha.slice(0, 12)}</dd></div><div><dt>Analyzed</dt><dd>{formatDate(analysis.created_at)}</dd></div></dl>
    <div className="ci-counts" aria-label="Aggregate change counts"><span>{counts.files_analyzed} analyzed</span><span>{counts.files_unchanged} unchanged</span><span>{counts.files_unavailable} unavailable</span><span>{counts.files_unsupported} unsupported</span><span>{counts.configuration_changes} changes</span><span>{counts.rule_findings} rule findings</span></div>
    <div className="ci-findings-heading"><h3>Sanitised findings</h3><span>{analysis.findings.length} listed</span></div>
    {analysis.findings.length ? <div className="table-wrap"><table><thead><tr><th>FILE</th><th>PARAMETER</th><th>CHANGE</th><th>RULES</th></tr></thead><tbody>{analysis.findings.map((finding, index) => <tr key={`${finding.file_path}-${finding.parameter}-${index}`}><td>{finding.file_path}</td><td>{finding.parameter}</td><td>{finding.change_type}</td><td>{finding.rule_names.join(', ') || 'Magnitude-based change'}</td></tr>)}</tbody></table></div> : <div className="no-results">This report contains no parameter-level findings.</div>}
    <p className="ci-advisory">Advisory pairwise change-risk result. It uses no historical baseline and does not predict deployment failure.</p>
  </section>
}

function ResultStatus({ status }: { status: HistoricalResult['status'] }) {
  const labels = { not_assessed_non_numeric: 'Not assessed', insufficient_history: 'Insufficient history', unusual: 'Unusual', within_historical_range: 'Within range' }
  return <span className={`result-status ${status}`}>{status === 'unusual' ? <TriangleAlert size={13} /> : status === 'within_historical_range' ? <CheckCircle2 size={13} /> : <AlertCircle size={13} />}{labels[status]}</span>
}

export default App
