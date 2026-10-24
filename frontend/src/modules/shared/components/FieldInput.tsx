import type { FormField } from "../types/api";

/** One input of a plugin's form (provider sign-in, router settings), described by the API. */
export function FieldInput({ field, value, onChange }: { field: FormField; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block text-sm font-medium">
      {field.label}
      {field.optional && <span className="font-normal text-muted"> (optional)</span>}
      <input
        className="field mt-1"
        type={field.kind === "password" ? "password" : field.kind === "email" ? "email" : "text"}
        inputMode={field.kind === "tel" ? "tel" : undefined}
        autoComplete={field.kind === "password" ? "off" : undefined}
        placeholder={field.placeholder}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      />
      {field.help && <span className="mt-1 block text-xs font-normal text-muted">{field.help}</span>}
    </label>
  );
}
