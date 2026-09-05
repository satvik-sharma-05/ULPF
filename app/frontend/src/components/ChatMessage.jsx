import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import RouteBadge from './RouteBadge.jsx'
import CypherBlock from './CypherBlock.jsx'
import RowsTable from './RowsTable.jsx'
import RowsChart from './RowsChart.jsx'
import ExportAnswer from './ExportAnswer.jsx'
import SpeakButton from './SpeakButton.jsx'
import DocumentCard from './chat/DocumentCard.jsx'
import EvidencePanel from './EvidencePanel.jsx'
import PlanTrace from './PlanTrace.jsx'
import { CHROME } from './analytics/theme.js'

const markdownComponents = {
  p: ({ children }) => <p className="mb-2 last:mb-0 leading-relaxed">{children}</p>,
  strong: ({ children }) => <strong className="font-semibold" style={{ color: CHROME.ink }}>{children}</strong>,
  ul: ({ children }) => <ul className="list-disc pl-5 mb-2 space-y-0.5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 mb-2 space-y-0.5">{children}</ol>,
  li: ({ children }) => <li className="leading-relaxed">{children}</li>,
  h1: ({ children }) => <h1 className="text-sm font-semibold mt-3 mb-1.5 first:mt-0" style={{ color: CHROME.ink }}>{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold mt-3 mb-1.5 first:mt-0" style={{ color: CHROME.ink }}>{children}</h2>,
  h3: ({ children }) => <h3 className="text-xs font-semibold mt-2 mb-1 first:mt-0" style={{ color: CHROME.ink }}>{children}</h3>,
  code: ({ children }) => (
    <code className="px-1 py-0.5 text-[11px] font-mono"
          style={{ background: CHROME.surfaceActive, color: CHROME.ink }}>
      {children}
    </code>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto mb-2" style={{ border: `1px solid ${CHROME.border}` }}>
      <table className="min-w-full text-[11px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead style={{ background: CHROME.surfaceRaised }}>{children}</thead>,
  th: ({ children }) => (
    <th className="px-3 py-1.5 text-left font-medium whitespace-nowrap"
        style={{ color: CHROME.inkMuted, borderBottom: `1px solid ${CHROME.border}` }}>
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="px-3 py-1.5 whitespace-nowrap" style={{ borderBottom: `1px solid ${CHROME.gridline}` }}>
      {children}
    </td>
  ),
  blockquote: ({ children }) => (
    <blockquote className="pl-3 italic mb-2"
                style={{ borderLeft: `2px solid ${CHROME.borderStrong}`, color: CHROME.inkMuted }}>
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-2" style={{ borderColor: CHROME.border }} />,
  a: ({ children, href }) => (
    <a href={href} target="_blank" rel="noreferrer" className="underline" style={{ color: CHROME.ink }}>
      {children}
    </a>
  ),
}

function MarkdownAnswer({ text }) {
  return (
    <div className="text-xs" style={{ color: CHROME.inkSecondary }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {text}
      </ReactMarkdown>
    </div>
  )
}

export function UserMessage({ text }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[80%] px-3.5 py-2 text-xs whitespace-pre-wrap"
           style={{ background: CHROME.accent, color: CHROME.accentInk }}>
        {text}
      </div>
    </div>
  )
}

export function BotMessage({ response, pending, question, mediaCaps }) {
  const shell = {
    background: CHROME.surface,
    border: `1px solid ${CHROME.border}`,
  }

  if (pending) {
    return (
      <div className="flex justify-start">
        <div className="max-w-[85%] px-3.5 py-2.5 text-xs" style={{ ...shell, color: CHROME.inkMuted }}>
          <span className="inline-flex gap-1 items-center">
            <span className="w-1.5 h-1.5 animate-bounce [animation-delay:-0.3s]" style={{ background: CHROME.inkMuted }} />
            <span className="w-1.5 h-1.5 animate-bounce [animation-delay:-0.15s]" style={{ background: CHROME.inkMuted }} />
            <span className="w-1.5 h-1.5 animate-bounce" style={{ background: CHROME.inkMuted }} />
          </span>
        </div>
      </div>
    )
  }

  if (response?.isError) {
    return (
      <div className="flex justify-start">
        <div className="max-w-[85%] px-3.5 py-2.5 text-xs"
             style={{ background: CHROME.surface, border: `1px solid ${CHROME.borderStrong}`, color: CHROME.ink }}>
          {response.answer}
        </div>
      </div>
    )
  }

  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] px-3.5 py-3 text-xs w-full" style={shell}>
        <div className="flex items-center gap-2 mb-1.5">
          <RouteBadge route={response.route} />
        </div>
        <MarkdownAnswer text={response.answer} />
        {/* A generated deck or report, when the message was a document
            request rather than a question about the graph. */}
        <DocumentCard doc={response.document} />
        {response.error && (
          <div className="mt-1.5 text-[10px]" style={{ color: CHROME.inkMuted }}>Note: {response.error}</div>
        )}
        <PlanTrace plan={response.plan} />
        <CypherBlock cypher={response.cypher} />
        {/* Chart first, then the table: the shape is the answer to "how has
            this changed", and the numbers are the detail underneath it.
            RowsChart returns null whenever the rows would not make an honest
            chart, so most answers still show the table alone. */}
        <RowsChart rows={response.rows} />
        <RowsTable rows={response.rows} />
        <EvidencePanel context={response.context} retrievalMethod={response.retrieval_method} />
        <div className="flex items-center gap-1.5 flex-wrap">
          <ExportAnswer question={question} response={response} />
          <SpeakButton text={response.answer} available={mediaCaps?.tts?.available} />
        </div>
      </div>
    </div>
  )
}
