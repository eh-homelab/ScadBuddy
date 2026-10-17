import { useState, type ReactNode } from 'react'
import { BoldIcon, ItalicIcon, UnderlineIcon } from 'lucide-react'
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '@/components/ui/accordion'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from '@/components/ui/command'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { HoverCard, HoverCardContent, HoverCardTrigger } from '@/components/ui/hover-card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet'
import { Skeleton } from '@/components/ui/skeleton'
import { Slider } from '@/components/ui/slider'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { Toggle } from '@/components/ui/toggle'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/**
 * #3653 — every shadcn component in src/components/ui on one page, for the eye and for
 * e2e/ui-gallery.spec.ts (the page CSP, portals inside Bambuddy's frame). Only in the
 * mocked build (`VITE_MOCK_API`, App.tsx): a production bundle has no route to it.
 */
export function UiGalleryPage() {
  const [volume, setVolume] = useState([40])
  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto flex max-w-5xl flex-col gap-8 p-6">
        <header>
          <h1 className="text-2xl font-semibold tracking-tight">Components</h1>
          <p className="text-sm text-muted-foreground">shadcn/ui on Radix, in ScadBuddy’s theme.</p>
        </header>

        <Section title="Buttons and badges">
          <div className="flex flex-wrap items-center gap-2">
            <Button>Primary</Button>
            <Button variant="secondary">Secondary</Button>
            <Button variant="outline">Outline</Button>
            <Button variant="ghost">Ghost</Button>
            <Button variant="destructive">Destructive</Button>
            <Button variant="link">Link</Button>
            <Button size="sm">Small</Button>
            <Button disabled>Disabled</Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge>Badge</Badge>
            <Badge variant="secondary">Secondary</Badge>
            <Badge variant="outline">Outline</Badge>
            <Badge variant="destructive">Destructive</Badge>
          </div>
        </Section>

        <Section title="Overlays">
          <div className="flex flex-wrap items-center gap-2">
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="outline">Dialog</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Print this plate?</DialogTitle>
                  <DialogDescription>It goes to the printer’s queue in Bambuddy.</DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <Button>Print</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            <AlertDialog>
              <AlertDialogTrigger asChild>
                <Button variant="outline">Alert dialog</Button>
              </AlertDialogTrigger>
              <AlertDialogContent>
                <AlertDialogHeader>
                  <AlertDialogTitle>Delete this model?</AlertDialogTitle>
                  <AlertDialogDescription>Its outputs and history go with it.</AlertDialogDescription>
                </AlertDialogHeader>
                <AlertDialogFooter>
                  <AlertDialogCancel>Cancel</AlertDialogCancel>
                  <AlertDialogAction>Delete</AlertDialogAction>
                </AlertDialogFooter>
              </AlertDialogContent>
            </AlertDialog>
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="outline">Sheet</Button>
              </SheetTrigger>
              <SheetContent>
                <SheetHeader>
                  <SheetTitle>Activity</SheetTitle>
                  <SheetDescription>What ran, and what it changed.</SheetDescription>
                </SheetHeader>
              </SheetContent>
            </Sheet>
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline">Popover</Button>
              </PopoverTrigger>
              <PopoverContent>
                <div className="grid gap-2">
                  <Label htmlFor="gallery-width">Width</Label>
                  <Input id="gallery-width" defaultValue="40 mm" />
                </div>
              </PopoverContent>
            </Popover>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline">Dropdown menu</Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuLabel>Model</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem>Duplicate</DropdownMenuItem>
                <DropdownMenuItem>Edit details</DropdownMenuItem>
                <DropdownMenuItem variant="destructive">Delete</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="outline">Tooltip</Button>
              </TooltipTrigger>
              <TooltipContent>Generate the model</TooltipContent>
            </Tooltip>
            <HoverCard>
              <HoverCardTrigger asChild>
                <Button variant="link">Hover card</Button>
              </HoverCardTrigger>
              <HoverCardContent>PLA Basic, Jade White: 212 g left.</HoverCardContent>
            </HoverCard>
            <ContextMenu>
              <ContextMenuTrigger className="flex h-9 items-center rounded-md border border-dashed px-3 text-sm">
                Right-click here
              </ContextMenuTrigger>
              <ContextMenuContent>
                <ContextMenuItem>Open</ContextMenuItem>
                <ContextMenuItem>Rename</ContextMenuItem>
              </ContextMenuContent>
            </ContextMenu>
          </div>
        </Section>

        <Section title="Form controls">
          <div className="grid gap-6 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor="gallery-name">Name</Label>
              <Input id="gallery-name" placeholder="Keychain" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="gallery-notes">Notes</Label>
              <Textarea id="gallery-notes" placeholder="What changed" />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="gallery-plate">Plate</Label>
              <Select defaultValue="textured">
                <SelectTrigger id="gallery-plate" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="textured">Textured PEI</SelectItem>
                  <SelectItem value="smooth">Smooth PEI</SelectItem>
                  <SelectItem value="cool">Cool plate</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label>Infill {volume[0]}%</Label>
              <Slider value={volume} onValueChange={setVolume} max={100} step={5} />
            </div>
            <div className="flex items-center gap-2">
              <Checkbox id="gallery-supports" defaultChecked />
              <Label htmlFor="gallery-supports">Supports</Label>
            </div>
            <div className="flex items-center gap-2">
              <Switch id="gallery-timelapse" />
              <Label htmlFor="gallery-timelapse">Timelapse</Label>
            </div>
            <RadioGroup defaultValue="auto" className="flex gap-4">
              <div className="flex items-center gap-2">
                <RadioGroupItem value="auto" id="gallery-auto" />
                <Label htmlFor="gallery-auto">Auto</Label>
              </div>
              <div className="flex items-center gap-2">
                <RadioGroupItem value="manual" id="gallery-manual" />
                <Label htmlFor="gallery-manual">Manual</Label>
              </div>
            </RadioGroup>
            <div className="flex items-center gap-2">
              <Toggle aria-label="Bold">
                <BoldIcon />
              </Toggle>
              <ToggleGroup type="single" variant="outline" defaultValue="italic">
                <ToggleGroupItem value="italic" aria-label="Italic">
                  <ItalicIcon />
                </ToggleGroupItem>
                <ToggleGroupItem value="underline" aria-label="Underline">
                  <UnderlineIcon />
                </ToggleGroupItem>
              </ToggleGroup>
            </div>
          </div>
        </Section>

        <Section title="Layout">
          <Tabs defaultValue="plates">
            <TabsList>
              <TabsTrigger value="plates">Plates</TabsTrigger>
              <TabsTrigger value="filaments">Filaments</TabsTrigger>
            </TabsList>
            <TabsContent value="plates" className="text-sm text-muted-foreground">
              Two plates, 3 h 12 min.
            </TabsContent>
            <TabsContent value="filaments" className="text-sm text-muted-foreground">
              Four colours.
            </TabsContent>
          </Tabs>
          <div className="grid gap-4 sm:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Name keychain</CardTitle>
                <CardDescription>Two colours, 12 minutes.</CardDescription>
              </CardHeader>
              <CardContent>
                <Skeleton className="h-24 w-full" />
              </CardContent>
              <CardFooter>
                <Button size="sm">Customize</Button>
              </CardFooter>
            </Card>
            <ScrollArea className="h-48 rounded-lg border">
              <div className="p-3 text-sm">
                {Array.from({ length: 20 }, (_, i) => (
                  <div key={i} className="py-1">
                    Output {i + 1}
                  </div>
                ))}
              </div>
            </ScrollArea>
          </div>
          <Separator />
          <Accordion type="single" collapsible>
            <AccordionItem value="advanced">
              <AccordionTrigger>Advanced options</AccordionTrigger>
              <AccordionContent>Brim, ironing, prime tower.</AccordionContent>
            </AccordionItem>
          </Accordion>
          <Collapsible>
            <CollapsibleTrigger asChild>
              <Button variant="ghost" size="sm">
                Show details
              </Button>
            </CollapsibleTrigger>
            <CollapsibleContent className="text-sm text-muted-foreground">Rendered in 3.1 s.</CollapsibleContent>
          </Collapsible>
          <Command className="rounded-lg border">
            <CommandInput placeholder="Find an output…" />
            <CommandList>
              <CommandEmpty>No outputs.</CommandEmpty>
              <CommandGroup heading="Outputs">
                <CommandItem>Keychain, Luna</CommandItem>
                <CommandItem>Keychain, Sol</CommandItem>
                <CommandItem>Maze puzzle</CommandItem>
              </CommandGroup>
            </CommandList>
          </Command>
        </Section>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-lg font-medium">{title}</h2>
      {children}
    </section>
  )
}
