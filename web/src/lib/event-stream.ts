/**
 * Realtime SSE client for HivePilot's `GET /v1/events/stream` (HP-41).
 *
 * The browser's native `EventSource` can't send an `Authorization` header, and
 * this API is bearer-token authenticated (see `lib/api.ts`), so we stream over
 * `fetch` + a `ReadableStream` reader instead — which lets us attach the token
 * and a `Last-Event-ID` resume header. The frame parser is split out as a pure,
 * buffering function so partial network chunks (an event split across two
 * reads) are handled correctly and can be unit-tested without any I/O.
 */

/** One decoded SSE frame. `data` is the raw text; the caller JSON-parses it. */
export interface SseFrame {
  id?: string
  event?: string
  data: string
}

/** A change-bus event as delivered by `GET /v1/events/stream` (mirrors
 * `_format_sse` in `hivepilot/services/api_service.py`). */
export interface ChangeEvent {
  id: number
  kind: string
  entity_type: string
  entity_id: string
  tenant: string
  payload: unknown
}

/**
 * Create a stateful SSE parser. `feed(chunk)` returns every COMPLETE frame the
 * accumulated buffer now contains, keeping any trailing partial frame for the
 * next call. Comment-only frames (heartbeats — lines starting with `:`, no
 * `data`) are dropped.
 */
export function createSseParser(): { feed: (chunk: string) => SseFrame[] } {
  let buffer = ''
  return {
    feed(chunk: string): SseFrame[] {
      buffer += chunk
      const frames: SseFrame[] = []
      let sep = buffer.indexOf('\n\n')
      while (sep !== -1) {
        const block = buffer.slice(0, sep)
        buffer = buffer.slice(sep + 2)
        const frame = parseBlock(block)
        if (frame) frames.push(frame)
        sep = buffer.indexOf('\n\n')
      }
      return frames
    },
  }
}

function parseBlock(block: string): SseFrame | null {
  let id: string | undefined
  let event: string | undefined
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line === '' || line.startsWith(':')) continue // blank or comment (heartbeat)
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1) // SSE strips one leading space
    if (field === 'id') id = value
    else if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null // heartbeat / comment-only: nothing to deliver
  return { id, event, data: dataLines.join('\n') }
}

export interface StreamEventsOptions {
  onEvent: (event: ChangeEvent, rawId?: string) => void
  signal: AbortSignal
  getToken: () => string | null
  lastEventId?: string | null
  path?: string
  fetchImpl?: typeof fetch
}

/**
 * Open one SSE connection and pump decoded {@link ChangeEvent}s to `onEvent`
 * until the stream ends or `signal` aborts. Resolves when the server closes the
 * stream; rejects on a network/HTTP error so the caller can back off and
 * reconnect (passing the last seen `rawId` as `lastEventId` to resume).
 */
export async function streamEvents(opts: StreamEventsOptions): Promise<void> {
  const doFetch = opts.fetchImpl ?? fetch
  const headers: Record<string, string> = { Accept: 'text/event-stream' }
  const token = opts.getToken()
  if (token) headers.Authorization = `Bearer ${token}`
  if (opts.lastEventId) headers['Last-Event-ID'] = opts.lastEventId

  const response = await doFetch(opts.path ?? '/v1/events/stream', {
    headers,
    signal: opts.signal,
  })
  if (!response.ok || !response.body) {
    throw new Error(`event stream failed (HTTP ${response.status})`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = createSseParser()
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    const text = decoder.decode(value, { stream: true })
    for (const frame of parser.feed(text)) {
      try {
        const parsed = JSON.parse(frame.data) as ChangeEvent
        opts.onEvent(parsed, frame.id)
      } catch {
        // Malformed frame — skip it rather than tearing down the whole stream.
      }
    }
  }
}
