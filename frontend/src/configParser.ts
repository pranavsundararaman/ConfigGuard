import { parseDocument, stringify as stringifyYaml } from 'yaml'
import { api, type ConfigFormat } from './api.ts'

export type ParsedConfiguration = Record<string, unknown>
export type FormatSwitchChoice = 'convert' | 'example' | 'cancel'
export interface FormatSwitchPlan {
  sourceFormat: ConfigFormat
  targetFormat: ConfigFormat
  sourceContent: string
  convertedContent?: string
  conversionError?: string
  requiresChoice: boolean
}

const DEFAULT_EXAMPLES: Record<ConfigFormat, string> = {
  json: '{\n  "service": {\n    "replicas": 3,\n    "timeout_seconds": 30\n  },\n  "database": {\n    "pool_size": 12\n  }\n}',
  yaml: 'service:\n  replicas: 3\n  timeout_seconds: 30\ndatabase:\n  pool_size: 12\n',
  yml: 'service:\n  replicas: 3\n  timeout_seconds: 30\ndatabase:\n  pool_size: 12\n',
  env: 'SERVICE_REPLICAS=3\nSERVICE_TIMEOUT_SECONDS=30\nDATABASE_POOL_SIZE=12\n',
}

const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/
const ENV_SEGMENT = /^[A-Za-z][A-Za-z0-9_]*$/
const NUMERIC_TEXT = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i

function isObject(value: unknown): value is ParsedConfiguration {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function defaultConfigurationExample(format: ConfigFormat): string {
  return DEFAULT_EXAMPLES[format]
}

export async function parseConfiguration(content: string, format: ConfigFormat): Promise<ParsedConfiguration> {
  if (typeof content !== 'string') throw new Error('Configuration content must be text.')

  if (format === 'env') return (await api.parseConfig(content, format)).configuration

  if (format === 'json') {
    let value: unknown
    try {
      value = JSON.parse(content)
    } catch (error) {
      throw new Error(`Invalid JSON: ${error instanceof Error ? error.message : 'check the configuration syntax.'}`)
    }
    if (!isObject(value)) throw new Error('JSON root must be an object.')
    return value
  }

  if (format === 'yaml' || format === 'yml') {
    const document = parseDocument(content, { uniqueKeys: true, prettyErrors: true })
    if (document.errors.length > 0) throw new Error(`Invalid ${format.toUpperCase()}: ${document.errors[0].message}`)
    const value: unknown = document.toJS()
    if ((value === null || value === undefined) && document.contents === null) return {}
    if (!isObject(value)) throw new Error(`${format.toUpperCase()} root must be an object or mapping.`)
    return value
  }

  throw new Error(`Unsupported configuration format: ${String(format)}. Choose JSON, YAML, YML, or ENV.`)
}

function renderEnv(configuration: ParsedConfiguration): string {
  const variables = new Map<string, string>()

  function visit(value: unknown, path: string[]) {
    if (Array.isArray(value)) throw new Error(`ENV cannot safely represent the array at '${path.join('.')}'.`)
    if (isObject(value)) {
      const entries = Object.entries(value)
      if (entries.length === 0) throw new Error(`ENV cannot represent the empty object at '${path.join('.') || '(root)'}'.`)
      for (const [key, child] of entries) {
        if (!ENV_SEGMENT.test(key) || key.startsWith('_') || key.endsWith('_') || key.includes('__')) {
          throw new Error(`The key '${[...path, key].join('.')}' cannot be represented as an ENV variable name.`)
        }
        visit(child, [...path, key])
      }
      return
    }
    if (path.length === 0) throw new Error('ENV requires named scalar configuration values.')
    if (value === null || (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean')) {
      throw new Error(`ENV cannot safely represent the value at '${path.join('.')}'.`)
    }
    if (typeof value === 'number' && !Number.isFinite(value)) {
      throw new Error(`ENV cannot represent the non-finite number at '${path.join('.')}'.`)
    }

    let output: string
    if (typeof value === 'string') {
      if ([...value].some((character) => {
        const code = character.charCodeAt(0)
        return code < 0x20 && code !== 0x09
      })) {
        throw new Error(`ENV cannot safely represent control characters at '${path.join('.')}'.`)
      }
      if (/^(?:true|false)$/i.test(value) || NUMERIC_TEXT.test(value.trim())) {
        throw new Error(`The string at '${path.join('.')}' would be normalized as a boolean or number in ENV.`)
      }
      output = JSON.stringify(value)
    } else {
      output = String(value)
    }

    const variable = path.join('_').toUpperCase()
    if (!ENV_KEY.test(variable)) throw new Error(`The key '${path.join('.')}' cannot be represented as an ENV variable name.`)
    if (variables.has(variable)) throw new Error(`Configuration keys collide at ENV variable '${variable}'.`)
    variables.set(variable, `${variable}=${output}`)
  }

  for (const [key, value] of Object.entries(configuration)) visit(value, [key])
  return variables.size ? `${[...variables.values()].join('\n')}\n` : '# No settings yet\n'
}

function renderFromEnv(configuration: ParsedConfiguration): ParsedConfiguration {
  const nested: ParsedConfiguration = Object.create(null) as ParsedConfiguration
  for (const [key, value] of Object.entries(configuration)) {
    if (!ENV_KEY.test(key) || key.startsWith('_') || key.endsWith('_') || key.includes('__')) {
      throw new Error(`ENV key '${key}' cannot be split into unambiguous path components.`)
    }
    const path = key.split('_').map((component) => component.toLowerCase())
    let current = nested
    for (let index = 0; index < path.length; index += 1) {
      const component = path[index]
      const isLeaf = index === path.length - 1
      const existing = Object.hasOwn(current, component) ? current[component] : undefined
      if (isLeaf) {
        if (existing !== undefined) throw new Error(`ENV variables collide at inferred path '${path.slice(0, index + 1).join('.')}'.`)
        current[component] = value
      } else {
        if (existing !== undefined && !isObject(existing)) {
          throw new Error(`ENV variables collide at inferred path '${path.slice(0, index + 1).join('.')}'.`)
        }
        if (existing === undefined) current[component] = Object.create(null) as ParsedConfiguration
        current = current[component] as ParsedConfiguration
      }
    }
  }
  return nested
}

export async function convertConfiguration(
  content: string,
  sourceFormat: ConfigFormat,
  targetFormat: ConfigFormat,
): Promise<string> {
  const configuration = await parseConfiguration(content, sourceFormat)
  const sourceObject = sourceFormat === 'env' && targetFormat !== 'env'
    ? renderFromEnv(configuration)
    : configuration

  if (targetFormat === 'json') return `${JSON.stringify(sourceObject, null, 2)}\n`
  if (targetFormat === 'yaml' || targetFormat === 'yml') return stringifyYaml(sourceObject, { indent: 2, lineWidth: 0 })
  if (targetFormat === 'env') return renderEnv(sourceObject)
  throw new Error(`Unsupported configuration format: ${String(targetFormat)}.`)
}

export async function planFormatSwitch(
  content: string,
  sourceFormat: ConfigFormat,
  targetFormat: ConfigFormat,
): Promise<FormatSwitchPlan> {
  try {
    const convertedContent = await convertConfiguration(content, sourceFormat, targetFormat)
    return {
      sourceFormat,
      targetFormat,
      sourceContent: content,
      convertedContent,
      requiresChoice: sourceFormat === 'env' || targetFormat === 'env',
    }
  } catch (error) {
    return {
      sourceFormat,
      targetFormat,
      sourceContent: content,
      conversionError: error instanceof Error ? error.message : 'The content cannot be converted safely.',
      requiresChoice: true,
    }
  }
}

export function resolveFormatSwitch(plan: FormatSwitchPlan, choice: FormatSwitchChoice): { format: ConfigFormat; content: string } {
  if (choice === 'cancel') return { format: plan.sourceFormat, content: plan.sourceContent }
  if (choice === 'example') return { format: plan.targetFormat, content: defaultConfigurationExample(plan.targetFormat) }
  if (!plan.convertedContent) throw new Error(plan.conversionError ?? 'This content cannot be converted safely.')
  return { format: plan.targetFormat, content: plan.convertedContent }
}
