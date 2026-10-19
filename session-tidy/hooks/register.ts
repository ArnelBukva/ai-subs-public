import type { EngineInterface, Register } from 'claude-code'

// Archives sessions with no message for N days in the account the Claude app has open, through the app's own
// archive action (archive_session on its ccd_session_mgmt server). The app holds that account's list in memory, so
// the ai-subs server's record-file archive never reached its sidebar; the app's own action changes it at once and
// sticks. The ai-subs server still covers the accounts the app does not have open.
// Settings are shared with that server: ~/.local/state/ai-subs/auto-archive.json, {"enabled": true, "days": 2}.

const SERVER = 'ccd_session_mgmt'
const DAY = 86_400_000
const HOUR = 3_600_000
const CHECK_EVERY = 10 * 60_000   // every open session checks; the shared timestamp lets one of them sweep per hour

type Listed = { sessionId: string; title?: string; cwd?: string; isArchived?: boolean; isRunning?: boolean;
                pinned?: boolean; lastActivityAt?: string }

const text = (r: { content: readonly { type: string; text?: string }[] }) =>
  r.content.map(b => (b.type === 'text' ? b.text ?? '' : '')).join('')

async function hasUnsavedWork($: EngineInterface, cwd: string | undefined) {
  if (!cwd || !cwd.includes('/.claude/worktrees/')) return false   // shared checkouts: archiving touches no files
  if (!(await $.fs.exists(cwd))) return false                         // worktree already gone: nothing to lose
  const git = await $.process.run(['git', '-C', cwd, 'status', '--porcelain'])
  return git.exitCode !== 0 || git.stdout.trim() !== ''              // unknown counts as unsaved
}

async function note($: EngineInterface, home: string | undefined, why: string) {
  await $.fs.write(`${home}/.local/state/ai-subs/session-tidy-last-check.json`,
    JSON.stringify({ at: await $.clock.now(), session: await $.session.id(), surface: await $.session.surface(), why }))
}

async function sweep($: EngineInterface) {
  const home = await $.env.get('HOME')
  let conf = { enabled: false, days: 2 }
  try {
    conf = { ...conf, ...JSON.parse(await $.fs.read(`${home}/.local/state/ai-subs/auto-archive.json`)) }
  } catch (err) {
    return note($, home, `settings unreadable: ${String(err).slice(0, 120)}`)
  }
  if (!conf.enabled) return note($, home, 'auto-archive is off')
  // The app asks before archiving unless the session runs in bypass or auto mode; this one sweeps unattended.
  const settings = (await $.settings.read()) as { permissions?: { defaultMode?: string } }
  if (!['bypassPermissions', 'auto'].includes(settings.permissions?.defaultMode ?? ''))
    return note($, home, `default mode ${settings.permissions?.defaultMode} asks before archiving`)

  const now = await $.clock.now()
  if (now - Number((await $.store.get('lastSweep')) ?? 0) < HOUR - 60_000) return note($, home, 'another session swept this hour')

  const status: { at: number; session: string; days: number; archived: string[]; skipped: string[]; error?: string } =
    { at: now, session: await $.session.id(), days: conf.days, archived: [], skipped: [] }
  try {
    const listed = await $.mcp.call(SERVER, 'list_sessions', { limit: 500 })
    if (listed.isError) throw new Error(text(listed).slice(0, 300))
    await $.store.set('lastSweep', now)   // only a session that reached the app holds the hour
    const cutoff = now - conf.days * DAY
    for (const s of JSON.parse(text(listed)) as Listed[]) {
      if (s.isArchived || s.isRunning || s.pinned || !s.lastActivityAt || Date.parse(s.lastActivityAt) >= cutoff) continue
      if (await hasUnsavedWork($, s.cwd)) {
        status.skipped.push(`${s.title}: unsaved work in its worktree`)
        continue
      }
      const done = await $.mcp.call(SERVER, 'archive_session', { session_id: s.sessionId,
        reason: `Auto-archive: no message in ${conf.days} days` })
      const said = text(done)
      if (done.isError || !said.startsWith('Archived')) status.skipped.push(`${s.title}: ${said.slice(0, 160)}`)
      else status.archived.push(s.title ?? s.sessionId)
    }
  } catch (err) {
    return note($, home, `the app's session tools did not answer: ${String(err).slice(0, 200)}`)
  }
  await $.fs.write(`${home}/.local/state/ai-subs/session-tidy.json`, JSON.stringify(status))
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    const result = await next(e)
    // The app's session tools exist in desktop sessions only; elsewhere the list call fails and the sweep records it.
    if (e.surface !== 'terminal') {
      void sweep($).catch(() => {})
      $.clock.every(CHECK_EVERY, () => void sweep($).catch(() => {}))
    }
    return result
  })
}
