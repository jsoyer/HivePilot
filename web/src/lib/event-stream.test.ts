import { describe, expect, it, vi } from 'vitest'
import { type ChangeEvent, createSseParser, streamEvents } from './event-stream'

describe('createSseParser', () => {
  it('parses a single complete frame', () => {
    const p = createSseParser()
    const frames = p.feed('id: 3\nevent: run.completed\ndata: {"status":"ok"}\n\n')
    expect(frames).toEqual([{ id: '3', event: 'run.completed', data: '{"status":"ok"}' }])
  })

  it('parses multiple frames in one chunk', () => {
    const p = createSseParser()
    const frames = p.feed('id: 1\nevent: a\ndata: x\n\nid: 2\nevent: b\ndata: y\n\n')
    expect(frames.map((f) => f.id)).toEqual(['1', '2'])
    expect(frames.map((f) => f.event)).toEqual(['a', 'b'])
  })

  it('buffers a frame split across chunks', () => {
    const p = createSseParser()
    expect(p.feed('id: 5\nevent: run.started\ndata: {"a":')).toEqual([])
    const frames = p.feed('1}\n\n')
    expect(frames).toEqual([{ id: '5', event: 'run.started', data: '{"a":1}' }])
  })

  it('drops heartbeat/comment-only blocks', () => {
    const p = createSseParser()
    expect(p.feed(': connected\n\n')).toEqual([])
    expect(p.feed(': keep-alive\n\n')).toEqual([])
    expect(p.feed('data: real\n\n')).toEqual([{ id: undefined, event: undefined, data: 'real' }])
  })

  it('strips exactly one leading space after the field colon', () => {
    const p = createSseParser()
    const [frame] = p.feed('data:  two-leading-spaces\n\n')
    expect(frame.data).toBe(' two-leading-spaces')
  })
})

function streamOf(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c))
      controller.close()
    },
  })
}

describe('streamEvents', () => {
  it('delivers parsed ChangeEvents with their raw id and sends auth + resume headers', async () => {
    const seen: Array<{ event: ChangeEvent; rawId?: string }> = []
    let sentHeaders: Record<string, string> = {}
    const fetchImpl = vi.fn(async (_url: string, init: RequestInit) => {
      sentHeaders = init.headers as Record<string, string>
      return {
        ok: true,
        status: 200,
        body: streamOf([
          ': connected\n\n',
          'id: 7\nevent: step.recorded\ndata: {"id":7,"kind":"step.recorded","entity_type":"run","entity_id":"42","tenant":"acme","payload":{"step":"plan"}}\n\n',
        ]),
      } as unknown as Response
    })

    await streamEvents({
      onEvent: (event, rawId) => seen.push({ event, rawId }),
      signal: new AbortController().signal,
      getToken: () => 'tok-123',
      lastEventId: '6',
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    expect(sentHeaders.Authorization).toBe('Bearer tok-123')
    expect(sentHeaders['Last-Event-ID']).toBe('6')
    expect(seen).toHaveLength(1)
    expect(seen[0].rawId).toBe('7')
    expect(seen[0].event.entity_type).toBe('run')
    expect(seen[0].event.entity_id).toBe('42')
    expect(seen[0].event.tenant).toBe('acme')
  })

  it('throws on a non-ok response so the caller can back off and reconnect', async () => {
    const fetchImpl = vi.fn(async () => ({ ok: false, status: 503, body: null }) as unknown as Response)
    await expect(
      streamEvents({
        onEvent: () => {},
        signal: new AbortController().signal,
        getToken: () => null,
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    ).rejects.toThrow(/503/)
  })

  it('skips a malformed data frame without tearing down the stream', async () => {
    const seen: ChangeEvent[] = []
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      status: 200,
      body: streamOf([
        'data: not-json\n\n',
        'id: 9\ndata: {"id":9,"kind":"run.started","entity_type":"run","entity_id":"1","tenant":"default","payload":null}\n\n',
      ]),
    }) as unknown as Response)

    await streamEvents({
      onEvent: (event) => seen.push(event),
      signal: new AbortController().signal,
      getToken: () => null,
      fetchImpl: fetchImpl as unknown as typeof fetch,
    })

    expect(seen).toHaveLength(1)
    expect(seen[0].id).toBe(9)
  })
})
