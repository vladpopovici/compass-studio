// shadcn/ui tabs (Radix), trimmed.
import { Tabs as TabsPrimitive } from "radix-ui";
import type * as React from "react";
import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return <TabsPrimitive.List className={cn("flex h-8 shrink-0 items-stretch border-b border-border", className)} {...props} />;
}

export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn("flex items-center gap-1.5 border-b-2 border-transparent px-3 text-muted-foreground outline-none " +
        "hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring data-[state=active]:border-primary " +
        "data-[state=active]:text-foreground", className)}
      {...props} />
  );
}

export function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn("min-h-0 flex-1 overflow-y-auto outline-none", className)} {...props} />;
}
