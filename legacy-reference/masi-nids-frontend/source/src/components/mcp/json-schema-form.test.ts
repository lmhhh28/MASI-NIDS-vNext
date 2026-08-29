import { describe, expect, it } from "vitest";

import { initialValueFromSchema, resolveSchema, validateSchemaValue } from "./json-schema-form";

const schema = {
  type: "object",
  $defs: {
    payload: {
      type: "object",
      properties: {
        mode: { type: "string", enum: ["safe", "strict"], default: "safe" },
        ttl: { anyOf: [{ type: "integer", minimum: 1, maximum: 60 }, { type: "null" }], default: null },
        tags: { type: "array", items: { type: "string", pattern: "^[a-z]+$" }, default: [] },
      },
      required: ["mode"],
    },
  },
  properties: {
    target: { type: "string", minLength: 2 },
    payload: { $ref: "#/$defs/payload" },
  },
  required: ["target", "payload"],
};

describe("JSON Schema renderer helpers", () => {
  it("resolves local refs and materializes nested defaults", () => {
    const resolved = resolveSchema(schema, schema.properties.payload);
    expect(resolved.type).toBe("object");
    expect(initialValueFromSchema(schema)).toEqual({ payload: { mode: "safe", ttl: null, tags: [] } });
  });

  it("validates required, enum, nullable, arrays, patterns and numeric bounds", () => {
    expect(validateSchemaValue(schema, { target: "sw", payload: { mode: "strict", ttl: null, tags: ["edge"] } })).toEqual([]);
    const errors = validateSchemaValue(schema, { target: "x", payload: { mode: "unsafe", ttl: 99, tags: ["BAD"] } });
    expect(errors).toEqual(expect.arrayContaining([
      "$.target: too short",
      "$.payload.mode: invalid enum value",
      "$.payload.ttl: above maximum",
      "$.payload.tags[0]: pattern mismatch",
    ]));
  });
});
