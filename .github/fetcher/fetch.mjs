// Scythe article fetcher (runs in GitHub Actions on issues labeled scythe:fetch).
//
// Loads the article as a real page in headless Chromium (an iframe can't be read cross-origin, and
// most news sites forbid framing), runs Mozilla Readability in the page, then strips the result to a
// small HTML allowlist and to plain paragraphs. Writes sources/<n>.md (verbatim text the cards are
// cut from) and sources/<n>.html (clean HTML), relabels the issue scythe:source. The workflow commits.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { chromium } from 'playwright'

const require = createRequire(import.meta.url)
const READABILITY = readFileSync(require.resolve('@mozilla/readability/Readability.js'), 'utf8')
const { GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_EVENT_PATH, GITHUB_API_URL = 'https://api.github.com' } = process.env

async function gh(method, path, body) {
  const res = await fetch(`${GITHUB_API_URL}${path}`, {
    method, body: body && JSON.stringify(body),
    headers: { Authorization: `Bearer ${GITHUB_TOKEN}`, Accept: 'application/vnd.github+json', 'Content-Type': 'application/json' },
  })
  if (!res.ok) throw new Error(`GitHub ${res.status} ${method} ${path}: ${await res.text()}`)
  return res.status === 204 ? null : res.json()
}

/** Same rules as the app and backend: NFC, no invisible characters, one blank line between paragraphs. */
export function sanitizeText(text) {
  return text.normalize('NFC').replace(/\u00ad|\u200b|\u200c|\u200d|\u2060|\ufeff/g, '').replace(/\r\n?/g, '\n')
    .split(/\n\s*\n/).map((b) => b.split('\n').map((l) => l.replace(/[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+/g, ' ').trim()).filter(Boolean).join(' '))
    .filter(Boolean).join('\n\n')
}

/** In the page: Readability, then keep only block text elements (no attributes but links). */
function extractInPage(readabilitySource) {
  // eslint-disable-next-line no-new-func
  new Function(readabilitySource + '\nwindow.__Readability = Readability;')()
  const article = new window.__Readability(document.cloneNode(true)).parse()
  if (!article) return null
  const holder = document.createElement('div')
  holder.innerHTML = article.content
  const KEEP = new Set(['P', 'H2', 'H3', 'H4', 'BLOCKQUOTE', 'LI', 'PRE'])
  const blocks = [...holder.querySelectorAll('p, h2, h3, h4, blockquote, li, pre')]
    .filter((el) => !el.parentElement.closest('p, h2, h3, h4, blockquote, li, pre'))
  const clean = blocks.map((el) => {
    const tag = KEEP.has(el.tagName) ? el.tagName.toLowerCase() : 'p'
    const text = el.textContent.replace(/\s+/g, ' ').trim()
    return text ? { tag, text } : null
  }).filter(Boolean)
  const meta = (name) => document.querySelector(`meta[property="${name}"], meta[name="${name}"]`)?.content ?? ''
  return {
    title: article.title || document.title, byline: article.byline || meta('author'), siteName: article.siteName || meta('og:site_name'),
    published: article.publishedTime || meta('article:published_time') || meta('date'), blocks: clean,
  }
}

const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c])

function diffableJson(obj) {
  const lines = Object.entries(obj).map(([k, v], i, all) => {
    const comma = i < all.length - 1 ? ',' : ''
    if (Array.isArray(v) && v.length) return `  ${JSON.stringify(k)}: [\n${v.map((x, j) => `    ${JSON.stringify(x)}${j < v.length - 1 ? ',' : ''}`).join('\n')}\n  ]${comma}`
    return `  ${JSON.stringify(k)}: ${JSON.stringify(v)}${comma}`
  })
  return `{\n${lines.join('\n')}\n}`
}

async function main() {
  const event = JSON.parse(readFileSync(GITHUB_EVENT_PATH, 'utf8'))
  const n = event.issue.number
  const url = /<!-- scythe:v1\n([\s\S]*?)\n-->/.exec(event.issue.body ?? '')?.[1]
  const target = url ? JSON.parse(url).url : null
  if (!target) throw new Error('no url in the issue metadata')

  const browser = await chromium.launch()
  try {
    const page = await (await browser.newContext({ javaScriptEnabled: true, userAgent: 'Mozilla/5.0 (scythe article fetcher)' })).newPage()
    await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 45000 })
    await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {})
    const got = await page.evaluate(extractInPage, READABILITY)
    if (!got || !got.blocks.length) throw new Error('no readable article text (paywall or script-only page?)')

    const text = sanitizeText(got.blocks.map((b) => b.text).join('\n\n'))
    const html = got.blocks.map((b) => `<${b.tag}>${esc(b.text)}</${b.tag}>`).join('\n')
    const authors = (got.byline || '').replace(/^by\s+/i, '').split(/\s*(?:,|;|\band\b)\s*/).filter(Boolean)
    const published = /^\d{4}-\d{2}-\d{2}/.exec(got.published || '')?.[0] ?? null
    const head = { issue: n, title: got.title.trim(), url: target, publisher: (got.siteName || new URL(target).hostname).trim(),
      published, authors, cite: '', fetched: new Date().toISOString() }

    mkdirSync('sources', { recursive: true })
    writeFileSync(`sources/${n}.md`, `---\n${diffableJson(head)}\n---\n\n${text}\n`)
    writeFileSync(`sources/${n}.html`, `<!doctype html><meta charset="utf-8"><title>${esc(head.title)}</title>\n<article>\n${html}\n</article>\n`)
    const meta = { kind: 'source', path: `sources/${n}.md`, text_path: `sources/${n}.md`, length: text.length, ...head }
    await gh('PATCH', `/repos/${GITHUB_REPOSITORY}/issues/${n}`, {
      title: head.title || target, labels: ['scythe:source'],
      body: `${[head.authors.join(', '), head.publisher, head.published].filter(Boolean).join(', ')}\n\n${target}\n\nFull text: [\`sources/${n}.md\`](../blob/main/sources/${n}.md), ${text.length.toLocaleString('en-US')} characters\n\n<!-- scythe:v1\n${JSON.stringify(meta).replace(/</g, '\\u003c').replace(/>/g, '\\u003e')}\n-->\n`,
    })
    console.log(`sources/${n}.md: ${text.length} characters`)
  } catch (err) {
    await gh('POST', `/repos/${GITHUB_REPOSITORY}/issues/${n}/comments`, { body: `Couldn't read ${target}: ${err.message}\n\nPaste the text in Scythe instead.` })
    await gh('PATCH', `/repos/${GITHUB_REPOSITORY}/issues/${n}`, { labels: ['scythe:fetch-failed'] })
    throw err
  } finally {
    await browser.close()
  }
}

main().catch((e) => { console.error(e); process.exit(1) })
