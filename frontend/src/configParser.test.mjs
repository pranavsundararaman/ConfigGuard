import assert from 'node:assert/strict'
import test from 'node:test'
import { api, getCIHistoryDisplayState } from './api.ts'
import { convertConfiguration, defaultConfigurationExample, parseConfiguration, planFormatSwitch, resolveFormatSwitch } from './configParser.ts'

function mockFetch(t, implementation) {
  const originalFetch = globalThis.fetch
  t.after(() => { globalThis.fetch = originalFetch })
  globalThis.fetch = implementation
}

test('format-specific defaults parse as their selected formats', async (t) => {
  assert.equal(defaultConfigurationExample('env'), 'SERVICE_REPLICAS=3\nSERVICE_TIMEOUT_SECONDS=30\nDATABASE_POOL_SIZE=12\n')
  assert.equal(defaultConfigurationExample('yaml'), 'service:\n  replicas: 3\n  timeout_seconds: 30\ndatabase:\n  pool_size: 12\n')
  mockFetch(t, async (_input, init) => {
    const request = JSON.parse(init.body)
    assert.equal(request.format, 'env')
    assert.equal(request.content, defaultConfigurationExample('env'))
    return new Response(JSON.stringify({ configuration: {
      SERVICE_REPLICAS: 3,
      SERVICE_TIMEOUT_SECONDS: 30,
      DATABASE_POOL_SIZE: 12,
    } }), { status: 200 })
  })
  assert.deepEqual(await parseConfiguration(defaultConfigurationExample('json'), 'json'), {
    service: { replicas: 3, timeout_seconds: 30 }, database: { pool_size: 12 },
  })
  assert.deepEqual(await parseConfiguration(defaultConfigurationExample('yaml'), 'yaml'), {
    service: { replicas: 3, timeout_seconds: 30 }, database: { pool_size: 12 },
  })
  assert.deepEqual(await parseConfiguration(defaultConfigurationExample('yml'), 'yml'), {
    service: { replicas: 3, timeout_seconds: 30 }, database: { pool_size: 12 },
  })
  assert.deepEqual(await parseConfiguration(defaultConfigurationExample('env'), 'env'), {
    SERVICE_REPLICAS: 3, SERVICE_TIMEOUT_SECONDS: 30, DATABASE_POOL_SIZE: 12,
  })
})

test('JSON preserves the existing object parsing behavior', async () => {
  assert.deepEqual(await parseConfiguration('{"service":{"replicas":3}}', 'json'), {
    service: { replicas: 3 },
  })
})

for (const format of ['yaml', 'yml']) {
  test(`${format.toUpperCase()} parses nested mappings and arrays`, async () => {
    assert.deepEqual(await parseConfiguration('service:\n  replicas: 3\n  zones:\n    - west\n    - east\n', format), {
      service: { replicas: 3, zones: ['west', 'east'] },
    })
  })

  test(`${format.toUpperCase()} accepts an empty/comment-only document as an empty object`, async () => {
    assert.deepEqual(await parseConfiguration('# no settings yet\n', format), {})
  })
}

test('ENV parsing delegates to the API parser and returns its normalized object', async (t) => {
  mockFetch(t, async (input, init) => {
    assert.equal(input, '/api/parse-config')
    assert.deepEqual(JSON.parse(init.body), { content: 'PORT=8080', format: 'env' })
    return new Response(JSON.stringify({ configuration: { PORT: 8080 } }), { status: 200 })
  })
  assert.deepEqual(await parseConfiguration('PORT=8080', 'env'), { PORT: 8080 })
})

test('malformed JSON and YAML return readable validation errors', async () => {
  await assert.rejects(parseConfiguration('{broken', 'json'), /Invalid JSON:/)
  await assert.rejects(parseConfiguration('service: [unterminated', 'yaml'), /Invalid YAML:/)
  await assert.rejects(parseConfiguration('service: [unterminated', 'yml'), /Invalid YML:/)
  await assert.rejects(parseConfiguration('- item\n- item', 'yaml'), /root must be an object or mapping/)
})

test('non-object roots and unknown formats are rejected', async () => {
  await assert.rejects(parseConfiguration('[1, 2]', 'json'), /JSON root must be an object/)
  await assert.rejects(parseConfiguration('a=1', 'toml'), /Unsupported configuration format/)
})

test('JSON to YAML and manually edited YAML to ENV conversions use current content', async () => {
  const json = '{"service":{"replicas":3,"timeout_seconds":30},"database":{"pool_size":12}}'
  const yaml = await convertConfiguration(json, 'json', 'yaml')
  assert.match(yaml, /^service:\n  replicas: 3\n  timeout_seconds: 30\n/)
  assert.equal(await convertConfiguration(json, 'json', 'env'), defaultConfigurationExample('env'))
  const editedYaml = 'service:\n  replicas: 5\n  timeout_seconds: 44\ndatabase:\n  pool_size: 9\n'
  assert.equal(await convertConfiguration(editedYaml, 'yaml', 'env'), defaultConfigurationExample('env').replace('REPLICAS=3', 'REPLICAS=5').replace('SECONDS=30', 'SECONDS=44').replace('SIZE=12', 'SIZE=9'))
})

test('ENV conversion applies a documented inferred hierarchy convention', async (t) => {
  mockFetch(t, async (_input, init) => {
    const request = JSON.parse(init.body)
    assert.equal(request.format, 'env')
    return new Response(JSON.stringify({ configuration: { SERVICE_TIMEOUT_SECONDS: 30 } }), { status: 200 })
  })
  assert.equal(await convertConfiguration('SERVICE_TIMEOUT_SECONDS=30\n', 'env', 'yaml'), 'service:\n  timeout:\n    seconds: 30\n')
})

test('ENV conversion detects inferred path collisions, arrays, and lossy string normalization', async (t) => {
  mockFetch(t, async (_input, init) => {
    const request = JSON.parse(init.body)
    assert.equal(request.format, 'env')
    return new Response(JSON.stringify({ configuration: { A: 1, A_B: 2 } }), { status: 200 })
  })
  await assert.rejects(convertConfiguration('A=1\nA_B=2\n', 'env', 'yaml'), /collide at inferred path/)
  await assert.rejects(convertConfiguration('{"service_timeout":{"seconds":1},"service":{"timeout_seconds":2}}', 'json', 'env'), /collide at ENV variable/)
  await assert.rejects(convertConfiguration('{"items":[1,2]}', 'json', 'env'), /cannot safely represent the array/)
  await assert.rejects(convertConfiguration('{"code":"123"}', 'json', 'env'), /normalized as a boolean or number/)
})

test('format switch choices preserve exact source on cancel and replace only on explicit example choice', async () => {
  const source = 'SERVICE_REPLICAS=3\r\nCUSTOM_TIMEOUT=30'
  const plan = await planFormatSwitch(source, 'env', 'yaml')
  assert.equal(plan.requiresChoice, true)
  assert.deepEqual(resolveFormatSwitch(plan, 'cancel'), { format: 'env', content: source })
  assert.deepEqual(resolveFormatSwitch(plan, 'example'), { format: 'yaml', content: defaultConfigurationExample('yaml') })
})

test('format switch conversion preserves structured numeric and boolean types', async () => {
  const source = '{"service":{"replicas":3,"enabled":true}}'
  const plan = await planFormatSwitch(source, 'json', 'yaml')
  assert.equal(plan.requiresChoice, false)
  const switched = resolveFormatSwitch(plan, 'convert')
  assert.equal(switched.format, 'yaml')
  assert.deepEqual(await parseConfiguration(switched.content, switched.format), { service: { replicas: 3, enabled: true } })
})

test('ENV JSON and malformed input errors come from the backend parser', async (t) => {
  mockFetch(t, async (_input, init) => {
    const request = JSON.parse(init.body)
    assert.equal(request.format, 'env')
    return new Response(JSON.stringify({ detail: 'Invalid ENV syntax on line 1. Use KEY=value assignments.' }), { status: 400 })
  })
  await assert.rejects(parseConfiguration('{"service":{}}', 'env'), /Invalid ENV syntax on line 1/)
  await assert.rejects(parseConfiguration('not an assignment', 'env'), /Invalid ENV syntax on line 1/)
})

test('historical API payload keeps the selected format and normalized configuration', async (t) => {
  mockFetch(t, async (input, init) => {
    assert.equal(input, '/api/projects/payments/historical-analysis')
    assert.deepEqual(JSON.parse(init.body), {
      configuration: { SERVICE_REPLICAS: 3 }, format: 'env', min_observations: 3,
    })
    return new Response(JSON.stringify({ id: 'analysis-1' }), { status: 201 })
  })
  await api.analyze('payments', { SERVICE_REPLICAS: 3 }, 'env', 3)
})

test('a converted editor state sends the matching selected format and configuration', async (t) => {
  const source = '{"service":{"replicas":3,"enabled":true}}'
  const plan = await planFormatSwitch(source, 'json', 'yaml')
  const switched = resolveFormatSwitch(plan, 'convert')
  mockFetch(t, async (input, init) => {
    assert.equal(input, '/api/projects/payments/historical-analysis')
    const body = JSON.parse(init.body)
    assert.equal(body.format, switched.format)
    assert.deepEqual(body.configuration, { service: { replicas: 3, enabled: true } })
    return new Response(JSON.stringify({ id: 'analysis-2' }), { status: 201 })
  })
  const config = await parseConfiguration(switched.content, switched.format)
  await api.analyze('payments', config, switched.format, 3)
})

test('CI history API uses the browser session and supports an empty history', async (t) => {
  mockFetch(t, async (input, init) => {
    assert.equal(input, '/api/projects/payments/ci-analyses')
    assert.equal(init.method, undefined)
    assert.equal(init.credentials, 'same-origin')
    assert.equal(init.headers.Authorization, undefined)
    return new Response('[]', { status: 200 })
  })

  assert.deepEqual(await api.ciAnalyses('payments'), [])
})

test('CI history detail API returns a sanitized PR record', async (t) => {
  const record = {
    id: 'ci-1', project_id: 'payments', repository: 'owner/service', pull_request_number: 27,
    pull_request_url: 'https://github.com/owner/service/pull/27', base_sha: 'a'.repeat(40), head_sha: 'b'.repeat(40),
    risk_score: 74.5, classification: 'HIGH_RISK', findings: [],
    change_counts: { files_analyzed: 1, files_unchanged: 0, files_unavailable: 0, files_unsupported: 0, configuration_changes: 1, rule_findings: 1 },
    created_at: '2026-10-24T00:00:00Z',
  }
  mockFetch(t, async (input, init) => {
    assert.equal(input, '/api/projects/payments/ci-analyses/ci-1')
    assert.equal(init.headers.Authorization, undefined)
    return new Response(JSON.stringify(record), { status: 200 })
  })

  assert.deepEqual(await api.ciAnalysis('payments', 'ci-1'), record)
})

test('CI history API exposes safe session authentication errors', async (t) => {
  mockFetch(t, async (_input, init) => {
    assert.equal(init.headers.Authorization, undefined)
    return new Response(JSON.stringify({ detail: 'Authentication required.' }), { status: 401 })
  })

  await assert.rejects(api.ciAnalyses('payments'), (error) => {
    assert.equal(error.message, 'Authentication required.')
    return true
  })
})

test('write requests include the session CSRF token and same-origin cookies', async (t) => {
  api.setCsrfToken('synthetic-csrf-token')
  mockFetch(t, async (_input, init) => {
    assert.equal(init.credentials, 'same-origin')
    assert.equal(init.headers['X-CSRF-Token'], 'synthetic-csrf-token')
    return new Response(JSON.stringify({ id: 'version-1' }), { status: 201 })
  })
  await api.saveVersion('payments', { timeout: 30 }, 'json')
  api.setCsrfToken(null)
})

test('auth session and selected-repository endpoints use same-origin sessions', async (t) => {
  let requestCount = 0
  mockFetch(t, async (input, init) => {
    assert.equal(init.credentials, 'same-origin')
    requestCount += 1
    if (input === '/api/auth/me') return new Response(JSON.stringify({ authenticated: true, user: { id: 'u1', login: 'sample-user' }, csrf_token: 'csrf' }), { status: 200 })
    if (input === '/api/github/repositories') return new Response(JSON.stringify([{ id: 7, full_name: 'sample-user/app' }]), { status: 200 })
    assert.equal(init.headers['X-CSRF-Token'], 'csrf')
    if (input === '/api/projects/select') {
      assert.deepEqual(JSON.parse(init.body), { repository_id: 7 })
      return new Response(JSON.stringify({ id: 'project-1', repository: 'sample-user/app' }), { status: 200 })
    }
    assert.equal(input, '/api/projects/activate')
    assert.deepEqual(JSON.parse(init.body), { project_id: 'project-1' })
    return new Response(JSON.stringify({ active_project_id: 'project-1' }), { status: 200 })
  })

  const session = await api.authMe()
  api.setCsrfToken(session.csrf_token)
  assert.equal(session.authenticated, true)
  assert.equal((await api.repositories())[0].full_name, 'sample-user/app')
  assert.equal((await api.selectProject(7)).id, 'project-1')
  assert.equal((await api.activateProject('project-1')).active_project_id, 'project-1')
  assert.equal(requestCount, 4)
  api.setCsrfToken(null)
})

test('CI history view state covers loading, empty, success, and error states', () => {
  assert.equal(getCIHistoryDisplayState(false, false, false, false), 'connect')
  assert.equal(getCIHistoryDisplayState(true, true, false, false), 'loading')
  assert.equal(getCIHistoryDisplayState(true, false, false, false), 'empty')
  assert.equal(getCIHistoryDisplayState(true, false, true, false), 'ready')
  assert.equal(getCIHistoryDisplayState(true, false, false, true), 'error')
})
