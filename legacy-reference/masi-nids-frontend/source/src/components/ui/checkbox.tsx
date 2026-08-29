"use client"

import * as React from "react"
import { Checkbox as CheckboxPrimitive } from "@base-ui/react/checkbox"
import { CheckIcon, MinusIcon } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * Accessible checkbox built on Base UI primitive.
 *
 * - Mixed/indeterminate is forwarded as `indeterminate` (Base UI handles
 *   the `aria-checked="mixed"` ARIA contract internally).
 * - Always provide `aria-label` (or pair with a visible `<label>`); we
 *   never strip the focus ring (`focus-visible:ring-2`).
 * - Cursor pointer + smooth `transition-colors`. No layout-shifting hover.
 */
function Checkbox({
  className,
  indeterminate,
  ...props
}: CheckboxPrimitive.Root.Props) {
  return (
    <CheckboxPrimitive.Root
      data-slot="checkbox"
      indeterminate={indeterminate}
      className={cn(
        "peer inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-[4px] border border-input bg-background text-primary-foreground shadow-xs transition-colors outline-none",
        "focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/60",
        "data-[checked]:border-primary data-[checked]:bg-primary",
        "data-[indeterminate]:border-primary data-[indeterminate]:bg-primary",
        "disabled:cursor-not-allowed disabled:opacity-50",
        className
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator
        data-slot="checkbox-indicator"
        className="flex items-center justify-center text-current"
      >
        {indeterminate ? (
          <MinusIcon className="size-3" aria-hidden="true" />
        ) : (
          <CheckIcon className="size-3" aria-hidden="true" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  )
}

export { Checkbox }
