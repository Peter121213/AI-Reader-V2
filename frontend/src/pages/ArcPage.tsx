import { useCallback, useEffect, useRef, useState } from "react"
import { useParams } from "react-router-dom"
import {
  fetchArcAnalysis,
  fetchArcStatus,
  startArcAnalysis,
  type ArcItem,
} from "@/api/client"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { trackEvent } from "@/lib/tracker"

export default function ArcPage() {
  const { novelId } = useParams<{ novelId: string }>()

  const [items, setItems] = useState<ArcItem[]>([])
  const [status, setStatus] = useState<{
    total_chapters: number
    total_windows: number
    analyzed_windows: number
    running: boolean
  } | null>(null)
  const [loading, setLoading] = useState(true)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const load = useCallback(async () => {
    if (!novelId) return
    try {
      const [m, s] = await Promise.all([
        fetchArcAnalysis(novelId),
        fetchArcStatus(novelId),
      ])
      setItems(m.items)
      setStatus(s)
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败")
    } finally {
      setLoading(false)
    }
  }, [novelId])

  useEffect(() => {
    if (!novelId) return
    setLoading(true)
    setError(null)
    trackEvent("view_story_arc")
    void load()
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [novelId, load])

  // 任务运行中时轮询进度
  useEffect(() => {
    if (!status?.running) {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
      return
    }
    if (pollRef.current) return
    pollRef.current = setInterval(() => {
      void load()
    }, 5000)
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }
  }, [status?.running, load])

  const handleStart = async () => {
    if (!novelId) return
    setStarting(true)
    setError(null)
    try {
      await startArcAnalysis(novelId)
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败")
    } finally {
      setStarting(false)
      void load()
    }
  }

  const analyzed = status?.analyzed_windows ?? 0
  const total = status?.total_windows ?? 0
  const running = status?.running ?? false

  return (
    <div className="mx-auto h-full max-w-5xl space-y-4 overflow-y-auto p-4 md:p-6">
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center justify-between gap-3">
            <span>大故事结构 · 20章窗口</span>
            <div className="flex items-center gap-2 text-sm font-normal">
              <span className="text-muted-foreground tabular-nums">
                {analyzed}/{total} 窗口
              </span>
              {running && (
                <span className="inline-flex items-center gap-1 text-amber-600">
                  <span className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
                  拆解中
                </span>
              )}
              <Button
                size="sm"
                onClick={() => void handleStart()}
                disabled={starting || running || analyzed >= total}
              >
                {analyzed >= total && !running
                  ? "已全部拆解"
                  : running
                    ? "拆解中…"
                    : analyzed > 0
                      ? "补拆剩余窗口"
                      : "开始拆解全书"}
              </Button>
            </div>
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          以 20 章为窗口拆解大故事结构：<b>1类角色</b>（剧情作用与故事发展）、
          <b>2类角色</b>（氛围铺垫 / 情绪渲染）、<b>主角关键发展节点</b>。
        </CardContent>
      </Card>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900 dark:bg-red-950/30 dark:text-red-400">
          {error}
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="py-10 text-center text-sm text-muted-foreground">加载中…</div>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-dashed p-10 text-center text-sm text-muted-foreground">
          还没有大故事结构拆解。点击上方「开始拆解全书」生成窗口结构。
        </div>
      ) : (
        <div className="space-y-4">
          {items.map((item) => (
            <ArcWindowCard key={item.arc_start} item={item} />
          ))}
        </div>
      )}
    </div>
  )
}

function ArcWindowCard({ item }: { item: ArcItem }) {
  const { arc } = item
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex flex-wrap items-baseline gap-3 text-base">
          <span className="shrink-0 text-muted-foreground tabular-nums">
            第{item.arc_start}–{item.arc_end}章
          </span>
          <span className="flex-1 text-sm font-normal leading-relaxed text-foreground">
            {arc.window_summary}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 pt-1">
        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            1类角色 · 剧情作用
          </h4>
          <ul className="space-y-3">
            {arc.primary_roles.map((r, i) => (
              <li key={i} className="rounded-md bg-muted/50 p-3">
                <div className="mb-1 flex flex-wrap items-baseline gap-2">
                  <span className="font-medium">{r.name}</span>
                  <span className="text-xs text-muted-foreground">{r.role}</span>
                </div>
                <div className="text-sm leading-relaxed text-foreground/90">
                  {r.development}
                </div>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            2类角色 · 氛围铺垫 / 情绪渲染
          </h4>
          <ul className="space-y-2">
            {arc.secondary_roles.map((r, i) => (
              <li key={i} className="flex gap-2 text-sm leading-relaxed">
                <span className="shrink-0 font-medium">{r.name}</span>
                <span className="text-foreground/90">{r.function}</span>
              </li>
            ))}
          </ul>
        </section>

        <section>
          <h4 className="mb-2 text-sm font-semibold text-foreground">
            主角关键发展节点
          </h4>
          <ul className="space-y-2">
            {arc.protagonist_nodes.map((n, i) => (
              <li key={i} className="flex gap-2 text-sm leading-relaxed">
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                  第{n.chapter}章
                </span>
                <span className="text-foreground/90">
                  {n.event}
                  <span className="text-muted-foreground"> —— {n.impact}</span>
                </span>
              </li>
            ))}
          </ul>
        </section>
      </CardContent>
    </Card>
  )
}
