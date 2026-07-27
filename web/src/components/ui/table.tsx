"use client"

import * as React from "react"

import { useOverflowX } from "@/lib/use-overflow-x"
import { cn } from "@/lib/utils"

export interface TableProps extends React.ComponentProps<"table"> {
  /**
   * Accessible name for the horizontal scroll region, used ONLY when the
   * table actually overflows its container. Supply it for any table wide
   * enough to be cut off on a phone (Cost / Models / Agents); without it the
   * container is still keyboard-scrollable, it just isn't announced as a
   * named region — which is better than an unnamed `role="region"`.
   */
  scrollLabel?: string
}

/**
 * A table plus the scroll container that owns its overflow.
 *
 * The container has always been `overflow-x-auto`, which is what keeps a wide
 * table from scrolling the page body sideways. The mobile audit found the
 * other half of the problem: at 390px the Agents table is 717px wide inside a
 * 334px box (53% hidden), Models 660/334, Cost 584/334 — with no visible
 * affordance that anything was hidden, and no way to reach the hidden columns
 * from a keyboard, since the container was neither focusable nor labelled.
 *
 * When (and only when) the content actually overflows, the container becomes
 * a real scroll region: focusable, so arrow/Home/End keys scroll it, and
 * announced via `role="region"` when the caller supplied a `scrollLabel`.
 * The `table-scroll` class gives it the same always-visible slim scrollbar
 * the RunBoard Kanban already uses (see `index.css`) so the affordance is
 * visible without hover. On a desktop where nothing overflows, none of this
 * applies and no stray tab stop is introduced.
 */
function Table({ className, scrollLabel, ...props }: TableProps) {
  const containerRef = React.useRef<HTMLDivElement>(null)
  const scrollable = useOverflowX(containerRef)

  return (
    <div
      ref={containerRef}
      data-slot="table-container"
      data-scrollable={scrollable}
      className="table-scroll relative w-full overflow-x-auto"
      {...(scrollable
        ? {
            tabIndex: 0,
            ...(scrollLabel ? { role: "region", "aria-label": scrollLabel } : {}),
          }
        : {})}
    >
      <table
        data-slot="table"
        className={cn("w-full caption-bottom text-sm", className)}
        {...props}
      />
    </div>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<"thead">) {
  return (
    <thead
      data-slot="table-header"
      className={cn("[&_tr]:border-b", className)}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<"tbody">) {
  return (
    <tbody
      data-slot="table-body"
      className={cn("[&_tr:last-child]:border-0", className)}
      {...props}
    />
  )
}

function TableFooter({ className, ...props }: React.ComponentProps<"tfoot">) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
        "border-t bg-muted/50 font-medium [&>tr]:last:border-b-0",
        className
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<"tr">) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
        "border-b transition-colors hover:bg-muted/50 has-aria-expanded:bg-muted/50 data-[state=selected]:bg-muted",
        className
      )}
      {...props}
    />
  )
}

function TableHead({ className, ...props }: React.ComponentProps<"th">) {
  return (
    <th
      data-slot="table-head"
      className={cn(
        "h-10 px-2 text-left align-middle font-medium whitespace-nowrap text-foreground [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCell({ className, ...props }: React.ComponentProps<"td">) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
        "p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0",
        className
      )}
      {...props}
    />
  )
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<"caption">) {
  return (
    <caption
      data-slot="table-caption"
      className={cn("mt-4 text-sm text-muted-foreground", className)}
      {...props}
    />
  )
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
}
