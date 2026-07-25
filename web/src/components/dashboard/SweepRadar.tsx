import { useEffect, useRef } from 'react'
import { cn } from '@/lib/utils'

export type RadarAgentStatus = 'good' | 'warn' | 'crit' | 'idle'

export interface RadarAgent {
  id?: string
  label?: string
  status: RadarAgentStatus
}

export interface SweepRadarProps {
  agents: RadarAgent[]
  ariaLabel?: string
  className?: string
}

const STATUS_VAR: Record<RadarAgentStatus, string> = {
  good: '--good',
  warn: '--warn',
  crit: '--crit',
  idle: '--idle',
}

const STATUS_FALLBACK: Record<RadarAgentStatus, string> = {
  good: '#57a66b',
  warn: '#d89a4a',
  crit: '#e0483f',
  idle: '#9096a8',
}

const SWEEP_FALLBACK = '#4fb8c9'
const GRID_FALLBACK = '#2b303c'
const REVOLUTION_MS = 8000
const DEFAULT_SIZE = 240

/** Reads a theme CSS custom property from the live cascade (via
 * `getComputedStyle`) with a hardcoded fallback — canvas 2D drawing can't
 * consume Tailwind's `var(--x)` arbitrary-value classes the SVG primitives
 * use, so colors have to be resolved to real color strings at draw time.
 * Reading live means the sweep repaints correctly across the light/dark
 * toggle without any React re-render. */
function resolveVar(el: Element, name: string, fallback: string): string {
  const value = window.getComputedStyle(el).getPropertyValue(name).trim()
  return value || fallback
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/**
 * The signature "instrument" visualization: agents plotted by angle/radius
 * on a canvas radar, colored by status, with a slow accent sweep arc
 * (~8s/revolution). Canvas (not SVG) because it redraws every animation
 * frame — cheaper than diffing DOM nodes 60x/sec. Respects
 * `prefers-reduced-motion`: renders a single static frame with no sweep
 * arc and no `requestAnimationFrame` loop when set.
 */
export function SweepRadar({ agents, ariaLabel, className }: SweepRadarProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const reducedMotion = prefersReducedMotion()
    let frameId: number | null = null

    function draw(sweepAngle: number) {
      const size = canvas!.clientWidth || DEFAULT_SIZE
      const dpr = window.devicePixelRatio || 1
      if (canvas!.width !== size * dpr || canvas!.height !== size * dpr) {
        canvas!.width = size * dpr
        canvas!.height = size * dpr
      }
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)
      ctx!.clearRect(0, 0, size, size)

      const cx = size / 2
      const cy = size / 2
      const maxR = Math.max(0, size / 2 - 8)

      // Concentric range rings.
      ctx!.strokeStyle = resolveVar(canvas!, '--border', GRID_FALLBACK)
      ctx!.lineWidth = 1
      for (const fraction of [0.33, 0.66, 1]) {
        ctx!.beginPath()
        ctx!.arc(cx, cy, maxR * fraction, 0, Math.PI * 2)
        ctx!.stroke()
      }

      // Slow accent sweep arc (skipped entirely under reduced motion).
      if (!reducedMotion) {
        ctx!.save()
        ctx!.strokeStyle = resolveVar(canvas!, '--primary', SWEEP_FALLBACK)
        ctx!.globalAlpha = 0.6
        ctx!.lineWidth = 2
        ctx!.beginPath()
        ctx!.moveTo(cx, cy)
        ctx!.lineTo(cx + maxR * Math.cos(sweepAngle), cy + maxR * Math.sin(sweepAngle))
        ctx!.stroke()
        ctx!.restore()
      }

      // Agents, evenly spaced by angle; radius staggered by index (mod 3)
      // so they don't all land on a single ring.
      const total = agents.length
      agents.forEach((agent, index) => {
        const theta = total > 0 ? (index / total) * Math.PI * 2 : 0
        const radius = maxR * (0.3 + (index % 3) * 0.25)
        const x = cx + radius * Math.cos(theta)
        const y = cy + radius * Math.sin(theta)
        ctx!.fillStyle = resolveVar(canvas!, STATUS_VAR[agent.status], STATUS_FALLBACK[agent.status])
        ctx!.beginPath()
        ctx!.arc(x, y, 4, 0, Math.PI * 2)
        ctx!.fill()
      })
    }

    if (reducedMotion) {
      draw(0)
    } else {
      const start = performance.now()
      const loop = (now: number) => {
        const elapsed = now - start
        const angle = ((elapsed / REVOLUTION_MS) % 1) * Math.PI * 2
        draw(angle)
        frameId = window.requestAnimationFrame(loop)
      }
      frameId = window.requestAnimationFrame(loop)
    }

    return () => {
      if (frameId !== null) window.cancelAnimationFrame(frameId)
    }
  }, [agents])

  const critCount = agents.filter((agent) => agent.status === 'crit').length
  const warnCount = agents.filter((agent) => agent.status === 'warn').length
  const summary =
    ariaLabel ??
    `Agent radar: ${agents.length} agent${agents.length === 1 ? '' : 's'}, ${critCount} critical, ${warnCount} warning`

  return (
    <canvas
      ref={canvasRef}
      data-slot="sweep-radar"
      role="img"
      aria-label={summary}
      className={cn('aspect-square w-full', className)}
    />
  )
}
