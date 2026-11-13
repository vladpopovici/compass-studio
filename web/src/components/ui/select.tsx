// shadcn/ui select on Radix, trimmed.
import { Check, ChevronDown } from "lucide-react";
import { Select as SelectPrimitive } from "radix-ui";
import type * as React from "react";
import { cn } from "@/lib/utils";

export const Select = SelectPrimitive.Root;
export const SelectValue = SelectPrimitive.Value;

export function SelectTrigger({ className, children, ...props }:
  React.ComponentProps<typeof SelectPrimitive.Trigger>) {
  return (
    <SelectPrimitive.Trigger
      className={cn("flex h-7 items-center justify-between gap-2 rounded-md border border-border bg-transparent px-2 " +
                    "text-xs outline-none focus-visible:ring-2 focus-visible:ring-ring [&>span]:truncate", className)}
      {...props}>
      {children}
      <SelectPrimitive.Icon asChild><ChevronDown className="size-3.5 opacity-60" /></SelectPrimitive.Icon>
    </SelectPrimitive.Trigger>
  );
}

export function SelectContent({ className, children, position = "popper", ...props }:
  React.ComponentProps<typeof SelectPrimitive.Content>) {
  return (
    <SelectPrimitive.Portal>
      <SelectPrimitive.Content position={position} sideOffset={4}
        className={cn("z-50 max-h-80 min-w-[var(--radix-select-trigger-width)] overflow-hidden rounded-md border " +
                      "border-border bg-popover text-popover-foreground shadow-md", className)} {...props}>
        <SelectPrimitive.Viewport className="p-1">{children}</SelectPrimitive.Viewport>
      </SelectPrimitive.Content>
    </SelectPrimitive.Portal>
  );
}

export function SelectItem({ className, children, ...props }: React.ComponentProps<typeof SelectPrimitive.Item>) {
  return (
    <SelectPrimitive.Item
      className={cn("relative flex cursor-default select-none items-center rounded-sm py-1 pl-2 pr-7 text-xs " +
                    "outline-none data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground", className)}
      {...props}>
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      <span className="absolute right-2 flex size-3.5 items-center justify-center">
        <SelectPrimitive.ItemIndicator><Check className="size-3.5" /></SelectPrimitive.ItemIndicator>
      </span>
    </SelectPrimitive.Item>
  );
}
