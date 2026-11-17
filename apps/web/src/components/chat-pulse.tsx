"use client";

import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip } from "recharts";
import { type ChatBucket, timeOfDay } from "@/lib/format";

const SLOTS = 60; // ten minutes of 10-second buckets: the strip keeps the same time scale while it fills up

function PulseTooltip({ active, payload }: { active?: boolean; payload?: ReadonlyArray<{ payload: ChatBucket }> }) {
  const bucket = active ? payload?.[0]?.payload : undefined;
  if (!bucket) return null;
  return (
    <div className="rounded-md bg-popover px-2.5 py-1.5 text-xs shadow-lg">
      <div className="tnum text-muted-foreground">{timeOfDay(bucket.at * 1000, true)}</div>
      <div className="mt-0.5 font-medium">{bucket.chatters === null ? "Chat non lu" : `${bucket.chatters} personne(s) en 10 s`}</div>
      {bucket.spike ? (
        <div className="mt-1 flex items-center gap-1.5">
          <span className="size-1.5 rounded-[1px] bg-primary" />
          Pic détecté
        </div>
      ) : null}
    </div>
  );
}

/**
 * Who was talking in chat, bucket by bucket. The signal colour is spent on one thing only: the bucket
 * whose spike became a moment. A bucket during which chat was not read is a gap, never a zero.
 */
export function ChatPulse({ buckets }: { buckets: ChatBucket[] }) {
  const first = buckets[0];
  const lead = Math.max(0, SLOTS - buckets.length);
  const data: ChatBucket[] = first
    ? [...Array.from({ length: lead }, (_, i) => ({ at: first.at - (lead - i) * 10, chatters: null, spike: false })), ...buckets]
    : [];
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 2, right: 0, bottom: 0, left: 0 }} barCategoryGap={1}>
        <Tooltip cursor={{ fill: "var(--raised)" }} content={<PulseTooltip />} filterNull={false} isAnimationActive={false} />
        <Bar
          dataKey="chatters"
          radius={[2, 2, 0, 0]}
          maxBarSize={8}
          isAnimationActive={false}
          // A known bucket always leaves a mark, even at zero: only an unread one is empty.
          minPointSize={(value) => (value === null ? 0 : 2)}
        >
          {data.map((bucket) => (
            <Cell key={bucket.at} fill={bucket.spike ? "var(--primary)" : "var(--muted-foreground)"} fillOpacity={bucket.spike ? 1 : 0.5} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}
