"use client";

import { useId } from "react";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export type JsonSchema = Record<string, unknown>;

function record(value: unknown): JsonSchema {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as JsonSchema
    : {};
}

export function resolveSchema(root: JsonSchema, input: JsonSchema): JsonSchema {
  const reference = typeof input.$ref === "string" ? input.$ref : null;
  let resolved = input;
  if (reference?.startsWith("#/$defs/")) {
    const name = decodeURIComponent(reference.slice("#/$defs/".length));
    resolved = { ...record(record(root.$defs)[name]), ...input };
    delete resolved.$ref;
  }
  const alternatives = Array.isArray(resolved.anyOf)
    ? resolved.anyOf
    : Array.isArray(resolved.oneOf) ? resolved.oneOf : null;
  if (alternatives) {
    const selected = alternatives.map(record).find((item) => item.type !== "null") ?? {};
    const merged: JsonSchema = { ...selected, ...resolved, nullable: alternatives.some((item) => record(item).type === "null") };
    delete merged.anyOf;
    delete merged.oneOf;
    return resolveSchema(root, merged);
  }
  return resolved;
}

export function initialValueFromSchema(schema: JsonSchema, root: JsonSchema = schema): unknown {
  const node = resolveSchema(root, schema);
  if (Object.prototype.hasOwnProperty.call(node, "default")) return node.default;
  if (node.type === "object" || node.properties) {
    const result: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(record(node.properties))) {
      const value = initialValueFromSchema(record(child), root);
      if (value !== undefined) result[key] = value;
    }
    return result;
  }
  if (node.type === "array") return [];
  return undefined;
}

export function validateSchemaValue(
  schema: JsonSchema,
  value: unknown,
  root: JsonSchema = schema,
  path = "$",
): string[] {
  const node = resolveSchema(root, schema);
  const nullable = node.nullable === true;
  if (value == null) return value === null && nullable ? [] : [`${path}: required`];
  const errors: string[] = [];
  const type = node.type;
  if ((type === "object" || node.properties) && (typeof value !== "object" || Array.isArray(value))) {
    return [`${path}: expected object`];
  }
  if (type === "array" && !Array.isArray(value)) return [`${path}: expected array`];
  if (type === "string" && typeof value !== "string") return [`${path}: expected string`];
  if ((type === "integer" || type === "number") && typeof value !== "number") return [`${path}: expected number`];
  if (type === "boolean" && typeof value !== "boolean") return [`${path}: expected boolean`];

  const choices = Array.isArray(node.enum) ? node.enum : null;
  if (choices && !choices.some((choice) => Object.is(choice, value))) errors.push(`${path}: invalid enum value`);
  if (typeof value === "string") {
    if (typeof node.minLength === "number" && value.length < node.minLength) errors.push(`${path}: too short`);
    if (typeof node.maxLength === "number" && value.length > node.maxLength) errors.push(`${path}: too long`);
    if (typeof node.pattern === "string" && !new RegExp(node.pattern).test(value)) errors.push(`${path}: pattern mismatch`);
  }
  if (typeof value === "number") {
    if (typeof node.minimum === "number" && value < node.minimum) errors.push(`${path}: below minimum`);
    if (typeof node.maximum === "number" && value > node.maximum) errors.push(`${path}: above maximum`);
  }
  if (Array.isArray(value)) {
    if (typeof node.minItems === "number" && value.length < node.minItems) errors.push(`${path}: too few items`);
    if (typeof node.maxItems === "number" && value.length > node.maxItems) errors.push(`${path}: too many items`);
    const itemSchema = record(node.items);
    value.forEach((item, index) => errors.push(...validateSchemaValue(itemSchema, item, root, `${path}[${index}]`)));
  }
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const objectValue = value as Record<string, unknown>;
    const properties = record(node.properties);
    const required = Array.isArray(node.required) ? node.required.filter((item): item is string => typeof item === "string") : [];
    for (const name of required) {
      if (!(name in objectValue) || objectValue[name] === undefined || objectValue[name] === "") errors.push(`${path}.${name}: required`);
    }
    for (const [name, child] of Object.entries(properties)) {
      if (objectValue[name] !== undefined && objectValue[name] !== "") {
        errors.push(...validateSchemaValue(record(child), objectValue[name], root, `${path}.${name}`));
      }
    }
  }
  return errors;
}

function ObjectJsonInput({ id, value, onChange }: { id: string; value: unknown; onChange: (value: unknown) => void }) {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? {}, null, 2);
  const invalid = typeof value === "string";
  return (
    <div className="space-y-1">
      <Textarea
        id={id}
        className="min-h-28 font-mono text-xs"
        value={text}
        aria-invalid={invalid}
        onChange={(event) => {
          try { onChange(JSON.parse(event.target.value)); }
          catch { onChange(event.target.value); }
        }}
      />
      {invalid ? <p className="text-xs text-destructive" role="alert">Invalid JSON object</p> : null}
    </div>
  );
}

function Field({
  root,
  schema,
  name,
  value,
  required,
  onChange,
  path,
}: {
  root: JsonSchema;
  schema: JsonSchema;
  name: string;
  value: unknown;
  required: boolean;
  onChange: (value: unknown) => void;
  path: string;
}) {
  const generatedId = useId();
  const id = `schema-${generatedId.replaceAll(":", "")}`;
  const node = resolveSchema(root, schema);
  const title = typeof node.title === "string" ? node.title : name;
  const description = typeof node.description === "string" ? node.description : null;
  const choices = Array.isArray(node.enum) ? node.enum : null;
  const properties = record(node.properties);

  if (node.type === "object" || Object.keys(properties).length > 0) {
    if (Object.keys(properties).length === 0) {
      return (
        <div className="space-y-1.5 md:col-span-2">
          <Label htmlFor={id}>{title}{required ? <span className="ml-1 text-destructive">*</span> : null}</Label>
          {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
          <ObjectJsonInput id={id} value={value} onChange={onChange} />
        </div>
      );
    }
    const objectValue = typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
    const childRequired = Array.isArray(node.required) ? node.required : [];
    return (
      <fieldset className="space-y-3 rounded-md border p-3 md:col-span-2">
        <legend className="px-1 text-sm font-medium">{title}{required ? <span className="ml-1 text-destructive">*</span> : null}</legend>
        {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
        <div className="grid gap-3 md:grid-cols-2">
          {Object.entries(properties).map(([childName, childSchema]) => (
            <Field
              key={childName}
              root={root}
              schema={record(childSchema)}
              name={childName}
              value={objectValue[childName]}
              required={childRequired.includes(childName)}
              path={`${path}.${childName}`}
              onChange={(next) => onChange({ ...objectValue, [childName]: next })}
            />
          ))}
        </div>
      </fieldset>
    );
  }

  if (node.type === "array") {
    const arrayValue = Array.isArray(value) ? value : [];
    const itemSchema = record(node.items);
    return (
      <fieldset className="space-y-2 rounded-md border p-3 md:col-span-2">
        <legend className="px-1 text-sm font-medium">{title}{required ? <span className="ml-1 text-destructive">*</span> : null}</legend>
        {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
        {arrayValue.map((item, index) => (
          <div key={`${path}-${index}`} className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <Field root={root} schema={itemSchema} name={`${name} ${index + 1}`} value={item} required onChange={(next) => onChange(arrayValue.map((current, itemIndex) => itemIndex === index ? next : current))} path={`${path}[${index}]`} />
            </div>
            <Button type="button" size="icon" variant="ghost" aria-label={`Remove ${title} ${index + 1}`} onClick={() => onChange(arrayValue.filter((_, itemIndex) => itemIndex !== index))}><Trash2 aria-hidden="true" /></Button>
          </div>
        ))}
        <Button type="button" size="sm" variant="outline" onClick={() => onChange([...arrayValue, initialValueFromSchema(itemSchema, root) ?? ""])}><Plus aria-hidden="true" />Add item</Button>
      </fieldset>
    );
  }

  if (node.type === "boolean") {
    return (
      <label className="flex min-h-10 items-center gap-2 rounded-md border px-3">
        <Checkbox checked={value === true} onCheckedChange={(checked) => onChange(checked === true)} />
        <span className="text-sm">{title}</span>
      </label>
    );
  }

  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{title}{required ? <span className="ml-1 text-destructive">*</span> : null}</Label>
      {description ? <p className="text-xs text-muted-foreground">{description}</p> : null}
      {choices ? (
        <Select value={value == null ? "" : String(value)} onValueChange={(next) => onChange(choices.find((choice) => String(choice) === next))}>
          <SelectTrigger id={id}><SelectValue placeholder={required ? "Select" : "Optional"} /></SelectTrigger>
          <SelectContent>{choices.map((choice) => <SelectItem key={String(choice)} value={String(choice)}>{String(choice)}</SelectItem>)}</SelectContent>
        </Select>
      ) : (
        <Input
          id={id}
          type={node.type === "integer" || node.type === "number" ? "number" : "text"}
          value={value == null ? "" : String(value)}
          required={required && node.nullable !== true}
          pattern={typeof node.pattern === "string" ? node.pattern : undefined}
          min={typeof node.minimum === "number" ? node.minimum : undefined}
          max={typeof node.maximum === "number" ? node.maximum : undefined}
          minLength={typeof node.minLength === "number" ? node.minLength : undefined}
          maxLength={typeof node.maxLength === "number" ? node.maxLength : undefined}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === "") onChange(undefined);
            else if (node.type === "integer") onChange(Number.parseInt(raw, 10));
            else if (node.type === "number") onChange(Number(raw));
            else onChange(raw);
          }}
        />
      )}
    </div>
  );
}

export function JsonSchemaFields({ schema, value, onChange }: { schema: JsonSchema; value: Record<string, unknown>; onChange: (value: Record<string, unknown>) => void }) {
  const root = schema;
  const node = resolveSchema(root, schema);
  const properties = record(node.properties);
  const required = Array.isArray(node.required) ? node.required : [];
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {Object.entries(properties).map(([name, child]) => (
        <Field key={name} root={root} schema={record(child)} name={name} value={value[name]} required={required.includes(name)} path={`$.${name}`} onChange={(next) => onChange({ ...value, [name]: next })} />
      ))}
    </div>
  );
}
