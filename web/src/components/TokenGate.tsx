import { type ReactNode, useCallback, useEffect, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { ApiAuthError, apiFetch, getToken, setToken } from '@/lib/api'

type GateStatus = 'checking' | 'signed-out' | 'signed-in'

interface TokenGateProps {
  children: ReactNode
}

/**
 * Auth plumbing for the Mirador web UI: gates `children` behind a stored
 * bearer token, validated against `GET /v1/plugins/health` (any `read`-role
 * token can call it — see hivepilot/services/api_service.py). Re-used
 * as-is by Sprint 3's real views; this sprint only wires the gate itself.
 */
export function TokenGate({ children }: TokenGateProps) {
  const [status, setStatus] = useState<GateStatus>('checking')
  const [error, setError] = useState<string | null>(null)
  const [inputValue, setInputValue] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    let cancelled = false

    async function validateStoredToken() {
      if (!getToken()) {
        if (!cancelled) setStatus('signed-out')
        return
      }
      try {
        await apiFetch('/v1/plugins/health')
        if (!cancelled) setStatus('signed-in')
      } catch {
        if (!cancelled) setStatus('signed-out')
      }
    }

    void validateStoredToken()
    return () => {
      cancelled = true
    }
  }, [])

  const handleSubmit = useCallback(
    async (event: React.FormEvent<HTMLFormElement>) => {
      event.preventDefault()
      const trimmed = inputValue.trim()
      if (!trimmed) return

      setSubmitting(true)
      setError(null)
      setToken(trimmed)
      try {
        await apiFetch('/v1/plugins/health')
        setStatus('signed-in')
      } catch (err) {
        if (err instanceof ApiAuthError) {
          setError('Invalid token — check the read token and try again.')
        } else {
          setError('Could not reach the HivePilot API. Try again.')
        }
      } finally {
        setSubmitting(false)
      }
    },
    [inputValue],
  )

  if (status === 'checking') {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-sm text-muted-foreground">
        Checking session…
      </div>
    )
  }

  if (status === 'signed-in') {
    return <>{children}</>
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>Mirador</CardTitle>
          <CardDescription>Enter your HivePilot read token to continue.</CardDescription>
        </CardHeader>
        <CardContent>
          <form className="flex flex-col gap-3" onSubmit={handleSubmit}>
            <Input
              type="password"
              autoComplete="off"
              placeholder="hp_..."
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              aria-label="HivePilot read token"
            />
            {error && (
              <p role="alert" className="text-sm text-destructive">
                {error}
              </p>
            )}
            <Button type="submit" disabled={submitting || !inputValue.trim()}>
              {submitting ? 'Checking…' : 'Continue'}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
