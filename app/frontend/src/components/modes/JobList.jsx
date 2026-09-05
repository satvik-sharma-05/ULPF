import { CHROME } from '../analytics/theme.js'
import { formatCompact } from '../analytics/format.js'

// Job state as a bracketed word, not a colored chip.
const JOB_STATUS = {
  queued: { tag: 'QUEUED', glyph: '·' },
  running: { tag: 'RUNNING', glyph: '>' },
  completed: { tag: 'DONE', glyph: '✓' },
  failed: { tag: 'FAILED', glyph: '×' },
}

export default function JobList({ jobs }) {
  return (
    <section style={{ background: CHROME.surface, border: `1px solid ${CHROME.border}` }}>
      <header className="px-4 py-2.5" style={{ borderBottom: `1px solid ${CHROME.border}` }}>
        <h3 className="text-xs font-semibold uppercase tracking-wide" style={{ color: CHROME.ink }}>
          Ingest jobs
        </h3>
        <p className="text-[10px]" style={{ color: CHROME.inkMuted }}>
          Most recent first. Jobs live in the API process, so they reset when it restarts.
        </p>
      </header>

      {!jobs || jobs.length === 0 ? (
        <div className="px-4 py-8 text-center text-xs" style={{ color: CHROME.inkMuted }}>No ingests yet</div>
      ) : (
        <ul>
          {jobs.map((job, i) => {
            const meta = JOB_STATUS[job.status] || JOB_STATUS.queued
            const summary = job.summary
            const fallbackVectors = summary?.embeddings && !summary?.embedding_model_loaded
            return (
              <li key={job.job_id} className="px-4 py-2.5"
                  style={{ borderTop: i === 0 ? 'none' : `1px solid ${CHROME.gridline}` }}>
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <div className="flex items-center gap-2 min-w-0">
                    <span className="w-4 text-center text-[10px] font-mono" style={{ color: CHROME.ink }} aria-hidden="true">
                      {meta.glyph}
                    </span>
                    <span className="text-xs font-mono truncate" style={{ color: CHROME.ink }}>{job.filename}</span>
                    <span className="text-[9px] font-mono uppercase tracking-wide shrink-0" style={{ color: CHROME.inkMuted }}>
                      [{meta.tag}]
                    </span>
                  </div>
                  <span className="text-[10px] shrink-0" style={{ color: CHROME.inkMuted }}>
                    {job.detected_format}
                  </span>
                </div>

                <div className="flex items-center gap-3 mt-1 text-[10px] font-mono flex-wrap" style={{ color: CHROME.inkSecondary }}>
                  <span>{formatCompact(job.records_parsed || 0)} parsed</span>
                  <span>{formatCompact(job.records_written || 0)} written</span>
                  {summary?.total_entities !== undefined && <span>{formatCompact(summary.total_entities)} entities</span>}
                  {summary?.elapsed_seconds !== undefined && <span>{summary.elapsed_seconds}s</span>}
                  <span>embeddings {job.embed ? 'on' : 'off'}</span>
                </div>

                {fallbackVectors && (
                  <p className="text-[10px] mt-1.5 px-2 py-1 leading-relaxed"
                     style={{ background: CHROME.surfaceActive, color: CHROME.inkSecondary }}>
                    <strong style={{ color: CHROME.ink }}>Warning — </strong>
                    the embedding model did not load, so these vectors are the deterministic hash
                    fallback; semantic search over this data will not be meaningful.
                  </p>
                )}

                {job.error && (
                  <p className="text-[10px] mt-1.5 px-2 py-1"
                     style={{ border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
                    {job.error}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}
