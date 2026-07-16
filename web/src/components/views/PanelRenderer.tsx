import { Badge } from '@/components/ui/badge'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import type {
  PanelData,
  PanelStatSection,
  PanelTableSection,
  PanelTextSection,
} from '@/lib/mirador-api'

const STAT_VARIANT: Record<'ok' | 'warn' | 'error', 'secondary' | 'outline' | 'destructive'> = {
  ok: 'secondary',
  warn: 'outline',
  error: 'destructive',
}

function StatSectionView({ section }: { section: PanelStatSection }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-2">
      <span className="font-medium">{section.label}</span>
      <span>{section.value}</span>
      {section.status && <Badge variant={STAT_VARIANT[section.status]}>{section.status}</Badge>}
    </div>
  )
}

function TableSectionView({ section }: { section: PanelTableSection }) {
  return (
    <Table>
      <TableHeader>
        <TableRow>
          {section.columns.map((column, index) => (
            // Untrusted, plugin-authored column names — no stable id, index
            // key is safe here since the column list itself is static per
            // render (see `PanelData`'s docstring in hivepilot/plugins.py).
            <TableHead key={index}>{column}</TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {section.rows.map((row, rowIndex) => (
          <TableRow key={rowIndex}>
            {row.map((cell, cellIndex) => (
              <TableCell key={cellIndex}>{cell}</TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  )
}

function TextSectionView({ section }: { section: PanelTextSection }) {
  // Untrusted, plugin-authored content (`PanelData`'s docstring,
  // `hivepilot/plugins.py`) — rendered as a plain JSX text child, which
  // React escapes automatically. NEVER use dangerouslySetInnerHTML here.
  return <p className="whitespace-pre-wrap text-sm">{section.content}</p>
}

interface PanelRendererProps {
  data: PanelData
}

/**
 * Generic, renderer-agnostic panel section renderer (Sprint 3 web surface) —
 * the web counterpart of the TUI's `hivepilot/ui/dashboard.py`
 * `_panel_stat_widget` / `_panel_table_widget` / `_panel_text_widget`.
 *
 * Section content (label/value/content/table cells) is plugin-authored and
 * UNTRUSTED (`PanelData`'s docstring, `hivepilot/plugins.py`): every value
 * below is rendered through plain JSX interpolation, which React escapes
 * automatically — this file must never use `dangerouslySetInnerHTML`.
 */
export function PanelRenderer({ data }: PanelRendererProps) {
  if (data.sections.length === 0) {
    return <p className="text-sm text-muted-foreground">No data.</p>
  }

  return (
    <div className="flex flex-col gap-3">
      {data.sections.map((section, index) => {
        if (section.kind === 'stat') {
          return <StatSectionView key={index} section={section} />
        }
        if (section.kind === 'table') {
          return <TableSectionView key={index} section={section} />
        }
        return <TextSectionView key={index} section={section} />
      })}
    </div>
  )
}
