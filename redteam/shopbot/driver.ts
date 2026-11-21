/**
 * Red-team driver for shop-support-bot. evalgate copies this file to
 * <bot>/.evalgate/driver.ts and runs it with the bot's own tsx, so the `@/`
 * aliases resolve and the real respond(), tools, system prompt, policy
 * chunking and embedder are what gets attacked. Only the database is
 * replaced by an in-memory store built from the bot's own seed data.
 *
 * Usage (from the bot dir):
 *   pnpm exec tsx .evalgate/driver.ts --cases cases.jsonl --model ID --repeats 1 \
 *     --out transcripts.jsonl --seed-out seed.json [--only rt01,rt02] [--turn-timeout-s 300]
 */
import { randomUUID } from "node:crypto";
import { appendFileSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { createEmbedder, type EmbedFn } from "@/lib/ai/embeddings";
import { chatProviderOptions, getChatModel, getEmbeddingModel } from "@/lib/ai/provider";
import { MAX_STEPS, PREFETCHED_POLICIES, respond, type RespondDeps } from "@/lib/chat/respond";
import { customers, orders as seedOrders, products as seedProducts } from "@/lib/db/seed-data";
import type { Env } from "@/lib/env";
import { normalizePhone, type OrderRecord } from "@/lib/orders";
import { chunkMarkdown, embeddingText, type PolicyChunk } from "@/lib/rag/chunk";
import {
  ticketCode,
  type Conversation,
  type NewTicket,
  type PolicyMatch,
  type ProductRecord,
  type StoredMessage,
  type SupportStore,
} from "@/lib/store/types";

const DAY_MS = 24 * 60 * 60 * 1000;
const EMBEDDING_MODEL = "qwen3-embedding:0.6b";
const SEARCH_STOPWORDS = new Set(["kopi", "coffee", "biji", "beans", "bean", "yang", "ada", "stok", "stock"]);

type SeedOrder = (typeof seedOrders)[number];
type Fixtures = {
  products?: Array<Partial<ProductRecord> & { sku: string }>;
  orders?: Array<Record<string, any> & { number: string }>;
  policy_chunks?: PolicyChunk[];
};
type Case = { id: string; turns: string[]; fixtures?: Fixtures };

function arg(name: string, fallback?: string): string | undefined {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 ? process.argv[i + 1] : fallback;
}

function log(message: string) {
  process.stderr.write(`${message}\n`);
}

function die(message: string): never {
  log(`redteam driver: ${message}`);
  process.exit(3);
}

// ---------------------------------------------------------------- seed data

function buildProducts(fixtures?: Fixtures): ProductRecord[] {
  const list: ProductRecord[] = seedProducts.map((p) => ({ ...p }));
  for (const patch of fixtures?.products ?? []) {
    const existing = list.find((p) => p.sku === patch.sku);
    if (existing) Object.assign(existing, patch);
    else {
      list.push({
        name: patch.sku,
        origin: "Jawa Barat",
        roast: "medium",
        process: "full wash",
        tastingNotes: "cokelat",
        weightGrams: 200,
        priceIdr: 100_000,
        stock: 10,
        ...patch,
      });
    }
  }
  return list;
}

/** Mirrors seed.ts: total = items + shipping, dates relative to now, phones normalized. */
function buildOrders(products: ProductRecord[], fixtures?: Fixtures): OrderRecord[] {
  const bySku = new Map(products.map((p) => [p.sku, p]));
  const byEmail = new Map(customers.map((c) => [c.email, c]));
  const now = Date.now();
  const daysAgo = (d: number) => new Date(now - d * DAY_MS);

  const records: OrderRecord[] = [];
  const toRecord = (o: SeedOrder, customer: { name: string; email: string; phone: string }): OrderRecord => {
    const lines = o.items.map((item) => {
      const product = bySku.get(item.sku);
      if (!product) throw new Error(`Unknown SKU ${item.sku}`);
      return { product, quantity: item.quantity };
    });
    const itemsTotal = lines.reduce((sum, l) => sum + l.product.priceIdr * l.quantity, 0);
    return {
      number: o.number,
      status: o.status,
      shippingCity: o.shippingCity,
      courier: o.courier ?? null,
      trackingNumber: o.trackingNumber ?? null,
      totalIdr: itemsTotal + o.shippingFeeIdr,
      placedAt: daysAgo(o.placedDaysAgo),
      shippedAt: o.shippedDaysAgo === undefined ? null : daysAgo(o.shippedDaysAgo),
      deliveredAt: o.deliveredDaysAgo === undefined ? null : daysAgo(o.deliveredDaysAgo),
      customer: { name: customer.name, email: customer.email, phone: normalizePhone(customer.phone) },
      items: lines.map((l) => ({ name: l.product.name, quantity: l.quantity })),
    };
  };

  for (const o of seedOrders) {
    const customer = byEmail.get(o.customerEmail);
    if (!customer) throw new Error(`Unknown customer ${o.customerEmail}`);
    records.push(toRecord(o, customer));
  }

  for (const patch of fixtures?.orders ?? []) {
    const existing = records.find((r) => r.number === patch.number);
    if (existing) {
      // Overrides for an existing order: shippingCity, status, courier, trackingNumber, customerName.
      const { customerName, ...rest } = patch;
      Object.assign(existing, rest);
      if (customerName) existing.customer = { ...existing.customer, name: customerName };
    } else {
      // A new order: needs `customer` {name,email,phone}; the other fields have defaults.
      const customer = patch.customer;
      if (!customer) throw new Error(`fixture order ${patch.number} needs a customer`);
      const base: SeedOrder = {
        number: patch.number,
        customerEmail: customer.email,
        status: patch.status ?? "processing",
        shippingCity: patch.shippingCity ?? "Bandung",
        shippingFeeIdr: patch.shippingFeeIdr ?? 18_000,
        courier: patch.courier,
        trackingNumber: patch.trackingNumber,
        placedDaysAgo: patch.placedDaysAgo ?? 1,
        shippedDaysAgo: patch.shippedDaysAgo,
        deliveredDaysAgo: patch.deliveredDaysAgo,
        items: patch.items ?? [{ sku: "PREANGER-FW-200", quantity: 1 }],
      };
      records.push(toRecord(base, customer));
    }
  }
  return records;
}

// ---------------------------------------------------------------- policies

type EmbeddedChunk = PolicyChunk & { embedding: number[] };

function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    dot += a[i]! * b[i]!;
    na += a[i]! * a[i]!;
    nb += b[i]! * b[i]!;
  }
  return na && nb ? dot / Math.sqrt(na * nb) : 0;
}

function loadPolicyChunks(): PolicyChunk[] {
  const dir = path.join(process.cwd(), "content", "policies");
  const chunks: PolicyChunk[] = [];
  for (const file of readdirSync(dir).filter((f) => f.endsWith(".md")).sort()) {
    chunks.push(...chunkMarkdown(file, readFileSync(path.join(dir, file), "utf8")));
  }
  if (chunks.length === 0) throw new Error(`No policy files in ${dir}`);
  return chunks;
}

// ---------------------------------------------------------------- store

class MemoryStore implements SupportStore {
  conversations: Conversation[] = [];
  messages: Array<StoredMessage & { conversationId: string; toolCalls?: string[] }> = [];
  tickets: Array<NewTicket & { id: number }> = [];

  constructor(
    private products: ProductRecord[],
    private orders: OrderRecord[],
    private chunks: EmbeddedChunk[],
  ) {}

  async getOrCreateConversation(channel: Conversation["channel"], externalId: string) {
    let conversation = this.conversations.find((c) => c.channel === channel && c.externalId === externalId);
    if (!conversation) {
      conversation = { id: randomUUID(), channel, externalId, status: "bot", failedLookups: 0 };
      this.conversations.push(conversation);
    }
    return { ...conversation }; // a copy, like a database row
  }
  async getRecentMessages(conversationId: string, limit: number) {
    return this.messages
      .filter((m) => m.conversationId === conversationId)
      .slice(-limit)
      .map(({ role, content }) => ({ role, content }));
  }
  async saveMessage(conversationId: string, message: StoredMessage & { toolCalls?: string[] }) {
    this.messages.push({ conversationId, ...message });
  }
  async markHandoff(conversationId: string) {
    this.conv(conversationId).status = "handoff";
  }
  async recordFailedLookup(conversationId: string) {
    return ++this.conv(conversationId).failedLookups;
  }
  async findOrder(orderNumber: string) {
    return this.orders.find((o) => o.number === orderNumber) ?? null;
  }
  async searchProducts(query: string | undefined, limit: number) {
    // Same matching as db-store.ts: name, origin or sku contains any term.
    const terms = (query ?? "")
      .toLowerCase()
      .split(/[^\p{L}\p{N}-]+/u)
      .filter((t) => t.length >= 3 && !SEARCH_STOPWORDS.has(t));
    const sorted = [...this.products].sort((a, b) => a.name.localeCompare(b.name));
    if (terms.length === 0) return sorted.slice(0, limit);
    return sorted
      .filter((p) => terms.some((t) => [p.name, p.origin, p.sku].some((f) => f.toLowerCase().includes(t))))
      .slice(0, limit);
  }
  async searchPolicies(embedding: number[], limit: number): Promise<PolicyMatch[]> {
    return this.chunks
      .map((c) => ({ source: c.source, heading: c.heading, content: c.content, similarity: cosine(c.embedding, embedding) }))
      .sort((a, b) => b.similarity - a.similarity)
      .slice(0, limit);
  }
  async createTicket(ticket: NewTicket) {
    const id = this.tickets.length + 1;
    this.tickets.push({ ...ticket, id });
    return { id, code: ticketCode(id) };
  }
  async countHit() {
    return { hits: 1, retryAfterSeconds: 60 };
  }
  async deleteExpiredHits() {}

  private conv(id: string): Conversation {
    const c = this.conversations.find((x) => x.id === id);
    if (!c) throw new Error(`No conversation ${id}`);
    return c;
  }
}

// ---------------------------------------------------------------- run

class OllamaDown extends Error {}

async function ollamaTags(baseUrl: string): Promise<{ name: string; digest: string }[]> {
  try {
    const res = await fetch(`${baseUrl}/api/tags`, { signal: AbortSignal.timeout(10_000) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return ((await res.json()) as { models: { name: string; digest: string }[] }).models;
  } catch (error) {
    throw new OllamaDown(`Ollama is not reachable at ${baseUrl}: ${error instanceof Error ? error.message : error}`);
  }
}

function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: NodeJS.Timeout;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Error(`timeout after ${ms / 1000}s (${what})`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

const plain = (value: unknown) => JSON.parse(JSON.stringify(value ?? null));

async function runTurn(deps: RespondDeps, externalId: string, text: string, timeoutMs: number) {
  const started = Date.now();
  const turn: Record<string, any> = { user: text, kind: null, text: "", tool_calls: [], errors: [] };
  const work = async () => {
    const reply = await respond(deps, { channel: "web", externalId, text });
    turn.kind = reply.kind;
    if (reply.kind !== "model") {
      turn.text = reply.text;
      return;
    }
    const result = reply.result;
    // Drain the stream; the same stream carries error and tool-error parts.
    for await (const part of result.stream as AsyncIterable<any>) {
      if (part.type === "error") turn.errors.push(String(part.error?.message ?? part.error));
      if (part.type === "tool-error") {
        turn.errors.push(`tool ${part.toolName}: ${String(part.error?.message ?? part.error)}`);
      }
    }
    turn.text = await result.text;
    const steps = await result.steps;
    turn.steps = steps.length;
    for (const step of steps) {
      const outputs = new Map<string, any>();
      for (const r of step.toolResults as any[]) outputs.set(r.toolCallId, r.output);
      for (const call of step.toolCalls as any[]) {
        turn.tool_calls.push({ name: call.toolName, input: plain(call.input), output: plain(outputs.get(call.toolCallId)) });
      }
    }
    // onEnd saves the assistant message; give it a tick so the next turn sees the history.
    await new Promise((r) => setTimeout(r, 0));
  };
  try {
    await withTimeout(work(), timeoutMs, "turn");
  } catch (error) {
    turn.errors.push(error instanceof Error ? error.message : String(error));
  }
  turn.ms = Date.now() - started;
  return turn;
}

async function main() {
  const casesPath = arg("cases") ?? die("--cases is required");
  const model = arg("model") ?? die("--model is required");
  const repeats = Number(arg("repeats", "1"));
  const outPath = arg("out") ?? die("--out is required");
  const seedOut = arg("seed-out");
  const only = arg("only")?.split(",").filter(Boolean);
  const turnTimeoutMs = Number(arg("turn-timeout-s", "300")) * 1000;
  const baseUrl = (arg("base-url") ?? "http://localhost:11434").replace(/\/$/, "");

  const cases: Case[] = readFileSync(casesPath, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l))
    .filter((c: Case) => !only || only.includes(c.id));
  if (cases.length === 0) die("no cases selected");

  const tags = await ollamaTags(baseUrl).catch((e: Error) => die(e.message));
  for (const need of [model, EMBEDDING_MODEL]) {
    if (!tags.some((t) => t.name === need || t.name === `${need}:latest`)) die(`${need} is not pulled on ${baseUrl}`);
  }

  // Hand-built env: no DATABASE_URL needed. Temperature and the rest stay at the app's defaults.
  const env = {
    DATABASE_URL: "postgres://unused:unused@localhost:5432/unused",
    AI_PROVIDER: "ollama",
    AI_MODEL: model,
    EMBEDDING_PROVIDER: "ollama",
    EMBEDDING_MODEL: EMBEDDING_MODEL,
    OLLAMA_BASE_URL: `${baseUrl}/api`,
    ADMIN_USER: "admin",
  } as Env;
  const chatModel = getChatModel(env);
  const embed: EmbedFn = createEmbedder(getEmbeddingModel(env));
  const providerOptions = chatProviderOptions(env);

  // Embed the real policy chunks once per process.
  const baseChunks = loadPolicyChunks();
  const embeddingCache = new Map<string, number[]>();
  const embedChunk = async (chunk: PolicyChunk): Promise<EmbeddedChunk> => {
    const text = embeddingText(chunk);
    let embedding = embeddingCache.get(text);
    if (!embedding) {
      embedding = await embed(text);
      embeddingCache.set(text, embedding);
    }
    return { ...chunk, embedding };
  };
  log(`embedding ${baseChunks.length} policy chunks...`);
  const embeddedBase: EmbeddedChunk[] = [];
  for (const chunk of baseChunks) embeddedBase.push(await embedChunk(chunk));

  if (seedOut) {
    const orders = buildOrders(buildProducts());
    writeFileSync(
      seedOut,
      JSON.stringify(
        {
          customers: customers.map((c) => ({ ...c, phone_normalized: normalizePhone(c.phone) })),
          orders: orders.map((o) => ({
            number: o.number,
            customer_name: o.customer.name,
            email: o.customer.email,
            phone: o.customer.phone,
            raw_phone: customers.find((c) => c.email === o.customer.email)?.phone ?? o.customer.phone,
            tracking_number: o.trackingNumber,
            shipping_city: o.shippingCity,
            status: o.status,
          })),
          products: seedProducts.map((p) => ({ sku: p.sku, name: p.name })),
        },
        null,
        2,
      ),
    );
  }

  const digest = tags.find((t) => t.name === model || t.name === `${model}:latest`)?.digest ?? null;
  const settings = {
    model,
    model_digest: digest,
    embedding_model: EMBEDDING_MODEL,
    base_url: baseUrl,
    provider_options: providerOptions ?? null,
    temperature: "not overridden (provider default, as in the app)",
    max_steps: MAX_STEPS,
    prefetched_policies: PREFETCHED_POLICIES,
    turn_timeout_s: turnTimeoutMs / 1000,
  };
  writeFileSync(outPath, "");
  writeFileSync(`${outPath}.meta.json`, JSON.stringify(settings, null, 2));

  const limiter = { check: () => ({ allowed: true as const }) };
  const total = cases.length * repeats;
  let done = 0;
  for (const c of cases) {
    const fixtures = c.fixtures;
    const extra = await Promise.all((fixtures?.policy_chunks ?? []).map(embedChunk));
    for (let repeat = 0; repeat < repeats; repeat++) {
      const started = Date.now();
      const products = buildProducts(fixtures);
      const store = new MemoryStore(products, buildOrders(products, fixtures), [...embeddedBase, ...extra]);
      const deps: RespondDeps = { store, model: chatModel, embed, limiter, providerOptions };
      const externalId = `rt-${c.id}-${repeat}-${randomUUID().slice(0, 8)}`;

      const turns: Record<string, any>[] = [];
      let fatal: string | null = null;
      for (const text of c.turns) {
        const turn = await runTurn(deps, externalId, text, turnTimeoutMs);
        turns.push(turn);
        if (turn.errors.length) {
          try {
            await ollamaTags(baseUrl);
          } catch (error) {
            fatal = (error as Error).message;
          }
          break; // a failed turn makes the later turns meaningless
        }
      }
      if (fatal) die(`aborting the run: ${fatal}`);

      const conv = store.conversations[0];
      const record = {
        case_id: c.id,
        repeat,
        model,
        wall_ms: Date.now() - started,
        turns,
        side_effects: {
          tickets: store.tickets.map((t) => plain(t)),
          conversation_status: conv?.status ?? null,
          failed_lookups: conv?.failedLookups ?? 0,
        },
        error: turns.flatMap((t) => t.errors).join(" | ") || null,
      };
      appendFileSync(outPath, `${JSON.stringify(record)}\n`);
      done++;
      log(`[${done}/${total}] ${c.id} repeat ${repeat} ${(record.wall_ms / 1000).toFixed(1)}s${record.error ? ` ERROR ${record.error.slice(0, 120)}` : ""}`);
    }
  }
}

main().catch((error) => {
  log(`redteam driver failed: ${error instanceof Error ? error.stack : error}`);
  process.exit(error instanceof OllamaDown ? 3 : 1);
});
