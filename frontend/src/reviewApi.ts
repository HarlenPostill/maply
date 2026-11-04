import { z } from 'zod';

const tagSchema = z.object({ value: z.string(), confidence: z.number().nullable(), evidence: z.string(), visibility: z.string() });
export const labelsSchema = z.object({
  schema_version: z.literal('maply-accessibility/1'),
  footpath: tagSchema.extend({ value: z.enum(['present', 'absent', 'unknown']) }),
  features: z.array(z.object({ id: z.string(), kind: z.enum(['footpath_section', 'driveway', 'kerb_ramp', 'crossing', 'hazard']), region: z.tuple([z.number(), z.number(), z.number(), z.number()]).nullable(), location_description: z.string(), tags: z.record(z.string(), tagSchema) })),
  notes: z.array(z.string()), review_reasons: z.array(z.string()),
});
const reviewSchema = z.object({
  id: z.string(), batch_id: z.string(), status: z.enum(['queued', 'running', 'ready', 'failed', 'confirmed', 'rejected', 'stale', 'cancelled']), proposed_labels: labelsSchema.nullable(),
  error: z.string().nullable(), model: z.string(), usage: z.record(z.string(), z.unknown()).nullable().optional(),
  created_at: z.string(), decided_at: z.string().nullable(),
});
export const itemSchema = z.object({
  id: z.string(), code: z.string(), street: z.string(), status: z.string(), flags: z.array(z.string()), note: z.string(),
  result: z.record(z.string(), z.unknown()), labels: labelsSchema.nullable(),
  image_url: z.string().nullable(), thumb_url: z.string().nullable(), review_image_url: z.string().nullable(),
  url: z.string(), footpath: z.string(), side: z.string(), lat: z.number(), lng: z.number(), heading: z.number(),
  review: reviewSchema.nullable(),
});
export const batchSchema = z.object({
  id: z.string(), status: z.enum(['running', 'paused', 'completed', 'cancelled']), model: z.string(), limit: z.number(), concurrency: z.number(), auto_confirm: z.boolean(),
  counts: z.record(z.string(), z.number()), created_at: z.string(),
});
export const summarySchema = z.object({
  counts: z.record(z.string(), z.number()), batches: z.array(batchSchema),
  provider: z.object({ model: z.string(), configured: z.boolean() }),
});
export const pageSchema = z.object({ items: z.array(itemSchema), total: z.number(), offset: z.number(), limit: z.number() });
export type ReviewItem = z.infer<typeof itemSchema>;
export type ReviewLabels = z.infer<typeof labelsSchema>;
export type ReviewSummary = z.infer<typeof summarySchema>;
export type ReviewBatch = z.infer<typeof batchSchema>;
export type ReviewPage = z.infer<typeof pageSchema>;
export type ReviewQueue = 'absence' | 'needs_check' | 'skipped' | 'no_imagery' | 'blocked' | 'recovery' | 'untagged' | 'ready' | 'confirmed';

export async function reviewRequest<T>(path: string, schema: z.ZodType<T>, options?: { body?: unknown; signal?: AbortSignal }): Promise<T> {
  const response = await fetch(path, {
    method: options?.body === undefined ? 'GET' : 'POST', signal: options?.signal,
    headers: options?.body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: options?.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data: unknown = await response.json();
  if (!response.ok) {
    const error = z.object({ detail: z.unknown() }).safeParse(data);
    throw new Error(error.success && typeof error.data.detail === 'string' ? error.data.detail : `Request failed (${response.status}).`);
  }
  const parsed = schema.safeParse(data);
  if (!parsed.success) throw new Error('Maply returned an unexpected review response. Reload after updating the app.');
  return parsed.data;
}
export const resultSchema = z.object({ ok: z.boolean(), status: z.string() });
export function errorMessage(error: unknown): string { return error instanceof Error ? error.message : 'The request could not be completed.'; }
