const SCHEMA = 'ops.workspace-setup.v1'
const SUPPORTED = new Set(['source-navigation', 'serena', 'typescript'])

export function takeFragmentToken(locationObject = window.location, historyObject = window.history) {
  const fragment = locationObject.hash.replace(/^#/, '')
  const token = new URLSearchParams(fragment).get('token') || ''
  if (fragment) historyObject.replaceState(null, '', locationObject.pathname + locationObject.search)
  return token
}

export function buildRequest(values, selected) {
  let args
  try {
    args = JSON.parse(values.launcherArgs || '[]')
  } catch {
    throw new Error('Launcher arguments must be a JSON array of strings.')
  }
  if (!Array.isArray(args) || args.length > 8 || !args.every((item) => typeof item === 'string')) {
    throw new Error('Launcher arguments must be a JSON array of up to eight strings.')
  }
  const capabilities = ['source-navigation', ...['serena', 'typescript'].filter((id) => selected.has(id))]
  const request = {
    schema_version: SCHEMA,
    repo_key: values.repoKey.trim(),
    repository: values.repository.trim(),
    output_root: values.outputRoot.trim(),
    launcher: { command: values.launcherCommand.trim(), args },
    capabilities,
  }
  if (values.revision.trim()) request.revision = values.revision.trim()
  if (selected.has('serena')) {
    request.serena = {
      command: values.serenaCommand.trim(),
      python: values.serenaPython.trim(),
      runtime_home: values.serenaHome.trim(),
    }
  }
  if (selected.has('typescript')) {
    request.typescript = {
      node: values.typescriptNode.trim(),
      module: values.typescriptModule.trim(),
      sha256: values.typescriptSha256.trim(),
    }
  }
  return request
}

function element(tag, className, textValue) {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (textValue !== undefined) node.textContent = String(textValue)
  return node
}

function appendLine(parent, label, value) {
  const row = element('div', 'detail-row')
  row.append(element('span', 'detail-label', label), element('span', 'detail-value', value))
  parent.append(row)
}

function appendDetails(parent, title, lines) {
  const details = element('details', 'technical-details')
  details.append(element('summary', '', title))
  const body = element('div', 'technical-body')
  for (const [label, value] of lines) appendLine(body, label, value)
  details.append(body)
  parent.append(details)
}

export function mountWorkspaceSetup(doc = document, fetcher = fetch) {
  const token = takeFragmentToken(doc.defaultView.location, doc.defaultView.history)
  const form = doc.getElementById('setup-form')
  const catalogArea = doc.getElementById('capabilities')
  const message = doc.getElementById('message')
  const planSection = doc.getElementById('plan-section')
  const planContent = doc.getElementById('plan-content')
  const resultSection = doc.getElementById('result-section')
  const resultContent = doc.getElementById('result-content')
  const planButton = doc.getElementById('plan-button')
  const applyButton = doc.getElementById('apply-button')
  const selected = new Set(['source-navigation'])
  let catalog = null
  let currentPlan = null
  let currentRequest = null
  let editVersion = 0
  let operation = 0
  let busy = ''
  let configured = false

  function showMessage(text, kind = 'error') {
    message.textContent = text
    message.className = `message ${kind}`
    message.hidden = !text
  }

  function setBusy(value) {
    busy = value
    planButton.disabled = !catalog || busy === 'apply'
    planButton.textContent = busy === 'plan' ? 'Reviewing plan…' : 'Review setup plan →'
    applyButton.disabled = !currentPlan || Boolean(busy)
    applyButton.textContent = busy === 'apply' ? 'Creating configuration…' : 'Create configuration →'
    for (const control of form.elements) {
      if (control === planButton) continue
      if (control.type === 'checkbox') control.disabled = busy === 'apply' || control.value === 'source-navigation' || !SUPPORTED.has(control.value)
      else control.disabled = busy === 'apply'
    }
  }

  async function api(path, body) {
    if (!token) throw new Error('The setup link is missing its access token. Open the link provided by the local setup command.')
    const response = await fetcher(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      cache: 'no-store',
    })
    let data
    try { data = await response.json() } catch { throw new Error('The local setup service returned an unreadable response.') }
    if (!response.ok || data?.status === 'error') throw new Error(data?.reason || 'The local setup service could not complete this step.')
    return data
  }

  function readValues() {
    const get = (id) => doc.getElementById(id).value
    return {
      repoKey: get('repo-key'), repository: get('repository'), outputRoot: get('output-root'),
      launcherCommand: get('launcher-command'), launcherArgs: get('launcher-args'), revision: get('revision'),
      serenaCommand: get('serena-command'), serenaPython: get('serena-python'), serenaHome: get('serena-home'),
      typescriptNode: get('typescript-node'), typescriptModule: get('typescript-module'),
      typescriptSha256: get('typescript-sha256'),
    }
  }

  function renderCatalog() {
    catalogArea.replaceChildren()
    for (const item of catalog.capabilities) {
      const supported = item.setup === 'supported' && SUPPORTED.has(item.id)
      const card = element('label', `capability-card${supported ? '' : ' unavailable'}`)
      const top = element('span', 'capability-top')
      const input = element('input')
      input.type = 'checkbox'
      input.value = item.id
      input.checked = selected.has(item.id)
      input.disabled = !supported || item.id === 'source-navigation'
      input.setAttribute('aria-label', `${item.label}: ${supported ? 'available' : 'requires separate setup'}`)
      if (supported && item.id !== 'source-navigation') input.addEventListener('change', () => {
        if (input.checked) selected.add(item.id)
        else selected.delete(item.id)
        updateProviderFields()
        invalidate()
      })
      top.append(input, element('span', 'capability-name', item.label))
      const state = !supported ? 'Separate setup needed' : configured && selected.has(item.id) ? 'Configured · unverified' : currentPlan && selected.has(item.id) ? 'Ready to configure' : item.id === 'source-navigation' ? 'Included' : 'Available'
      const pill = element('span', `state-pill${supported ? '' : ' muted'}`, state)
      const coverage = element('span', 'capability-description', item.coverage || 'Requires an independent setup adapter.')
      card.append(top, coverage, pill)
      catalogArea.append(card)
    }
  }

  function updateProviderFields() {
    doc.getElementById('serena-fields').hidden = !selected.has('serena')
    doc.getElementById('typescript-fields').hidden = !selected.has('typescript')
    for (const id of ['serena-command', 'serena-python', 'serena-home']) doc.getElementById(id).required = selected.has('serena')
    for (const id of ['typescript-node', 'typescript-module', 'typescript-sha256']) doc.getElementById(id).required = selected.has('typescript')
    if (selected.has('serena') || selected.has('typescript')) doc.querySelector('.advanced').open = true
  }

  function invalidate() {
    editVersion += 1
    currentPlan = null
    currentRequest = null
    configured = false
    planSection.hidden = true
    resultSection.hidden = true
    showMessage('')
    applyButton.disabled = true
    if (catalog) renderCatalog()
  }

  function renderPlan(plan) {
    planContent.replaceChildren()
    planContent.append(element('p', 'review-lead', 'This will create a new local configuration bundle for the selected code checkout.'))
    const summary = element('div', 'review-summary')
    appendLine(summary, 'Workspace', plan.request.repo_key)
    appendLine(summary, 'Source revision', plan.inputs.revision)
    appendLine(summary, 'Location', plan.output_root)
    appendLine(summary, 'Files', 'tooling.json · mcp.json · plan.json')
    appendLine(summary, 'Included', plan.request.capabilities.map((id) => catalog.capabilities.find((item) => item.id === id)?.label || id).join(', '))
    planContent.append(summary)
    const notice = element('div', 'limit-note')
    notice.append(element('strong', '', 'Configured does not mean verified.'))
    const list = element('ul')
    for (const gap of plan.gaps || []) list.append(element('li', '', gap))
    notice.append(list)
    planContent.append(notice)
    appendDetails(planContent, 'Technical details', [
      ['Plan ID', plan.plan_sha256],
      ['Code checkout', plan.request.repository],
      ['Source tree', plan.inputs.tree],
      ['Launcher', plan.launcher.command],
      ['Launcher arguments', JSON.stringify(plan.launcher.args)],
      ['Runtime verification', plan.runtime_verification],
    ])
    planSection.hidden = false
    renderCatalog()
  }

  function renderResult(result) {
    resultContent.replaceChildren()
    resultContent.append(element('p', 'review-lead', 'The reviewed bundle has been created. Register its MCP settings with your chosen host, then run a real tool check.'))
    const summary = element('div', 'review-summary')
    appendLine(summary, 'Bundle folder', result.bundle)
    appendLine(summary, 'Host registration file', `${result.bundle}/mcp.json`)
    appendLine(summary, 'Tooling configuration', `${result.bundle}/tooling.json`)
    appendLine(summary, 'Reviewed plan', `${result.bundle}/plan.json`)
    resultContent.append(summary)
    const note = element('div', 'limit-note')
    note.append(element('strong', '', 'Next step'))
    note.append(element('p', '', 'Open mcp.json, adapt its fragment to your host’s registration format, and run a selected tool to verify the provider. Host registration and runtime verification have not been performed here.'))
    resultContent.append(note)
    resultSection.hidden = false
    renderCatalog()
  }

  form.addEventListener('input', (event) => {
    if (event.target instanceof doc.defaultView.HTMLInputElement && event.target.type !== 'checkbox') invalidate()
  })
  form.addEventListener('submit', async (event) => {
    event.preventDefault()
    if (!catalog || busy === 'apply') return
    if (!form.reportValidity()) return
    let request
    try { request = buildRequest(readValues(), selected) } catch (error) { showMessage(error.message); return }
    const version = editVersion
    const sequence = ++operation
    currentPlan = null
    currentRequest = null
    configured = false
    planSection.hidden = true
    resultSection.hidden = true
    showMessage('')
    setBusy('plan')
    try {
      const plan = await api('/api/plan', { request })
      if (version !== editVersion || sequence !== operation) return
      if (!plan.plan_sha256 || !plan.inputs?.revision || !plan.output_root) throw new Error('The setup service returned an incomplete plan.')
      currentPlan = plan
      currentRequest = request
      renderPlan(plan)
      planSection.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (error) {
      if (version === editVersion && sequence === operation) showMessage(error.message)
    } finally {
      if (sequence === operation) setBusy('')
    }
  })

  applyButton.addEventListener('click', async () => {
    if (!currentPlan || !currentRequest || busy) return
    const version = editVersion
    const sequence = ++operation
    const request = currentRequest
    const expected_plan_sha256 = currentPlan.plan_sha256
    showMessage('')
    setBusy('apply')
    try {
      const result = await api('/api/apply', { request, expected_plan_sha256 })
      if (version !== editVersion || sequence !== operation) return
      if (result.status !== 'configured' || !result.bundle || result.plan_sha256 !== expected_plan_sha256) {
        throw new Error('The setup service returned an incomplete configuration result.')
      }
      configured = true
      currentPlan = null
      currentRequest = null
      renderResult(result)
      resultSection.scrollIntoView({ behavior: 'smooth', block: 'start' })
    } catch (error) {
      if (version === editVersion && sequence === operation) {
        currentPlan = null
        currentRequest = null
        planSection.hidden = true
        renderCatalog()
        showMessage(`${error.message} Review a new plan before creating configuration.`)
      }
    } finally {
      if (sequence === operation) setBusy('')
    }
  })

  if (!token) {
    catalogArea.replaceChildren(element('p', 'placeholder', 'Open the link provided by the local setup command to begin.'))
    showMessage('The setup link is missing its access token. Open the link provided by the local setup command.')
  } else {
    api('/api/catalog').then((data) => {
      if (!Array.isArray(data.capabilities)) throw new Error('The setup service returned an incomplete capability list.')
      catalog = data
      renderCatalog()
      setBusy('')
    }).catch((error) => {
      catalogArea.replaceChildren(element('p', 'placeholder', 'Available options could not be loaded. Open a new link from the local setup command to try again.'))
      showMessage(`${error.message} Open a new link from the local setup command to try again.`)
    })
  }
  return { invalidate }
}

if (typeof document !== 'undefined' && document.getElementById('setup-form')) mountWorkspaceSetup()
