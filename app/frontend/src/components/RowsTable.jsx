import { useState } from 'react'
import { CHROME } from './analytics/theme.js'

function formatCell(value) {
  if (value === null || value === undefined) return '—'
  if (Array.isArray(value)) return value.join(', ')
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function RowsTable({ rows }) {
  const [open, setOpen] = useState(true)
  if (!rows || rows.length === 0) return null
  const columns = Object.keys(rows[0])

  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-[10px] px-2 py-1"
        style={{ border: `1px solid ${CHROME.border}`, color: CHROME.inkSecondary }}
      >
        {open ? 'Hide' : 'Show'} {rows.length} row{rows.length === 1 ? '' : 's'}
      </button>
      {open && (
        <div className="mt-1.5 overflow-x-auto max-h-72 overflow-y-auto"
             style={{ border: `1px solid ${CHROME.border}` }}>
          <table className="min-w-full text-[11px]">
            <thead className="sticky top-0" style={{ background: CHROME.surfaceRaised }}>
              <tr>
                {columns.map((c) => (
                  <th key={c} className="px-3 py-1.5 text-left font-medium whitespace-nowrap"
                      style={{ color: CHROME.inkMuted, borderBottom: `1px solid ${CHROME.border}` }}>
                    {c}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i} style={{ borderTop: `1px solid ${CHROME.gridline}` }}>
                  {columns.map((c) => (
                    <td key={c} className="px-3 py-1.5 whitespace-nowrap font-mono"
                        style={{ color: CHROME.inkSecondary }}>
                      {formatCell(row[c])}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
