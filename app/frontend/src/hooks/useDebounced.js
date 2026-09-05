import { useEffect, useState } from 'react'

// Returns `value` only after it has stopped changing for `delay` ms.
//
// Exists because the log filters fire a fetch per keystroke otherwise: typing
// "timeout" produced seven requests, each hitting a CONTAINS scan over the
// whole Log label. Beyond the wasted load, the responses race - an earlier,
// slower query can resolve after a later one and overwrite the correct rows
// with results for a prefix of what was typed, which looks exactly like "the
// filter doesn't work".
export default function useDebounced(value, delay = 300) {
  const [debounced, setDebounced] = useState(value)

  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)
  }, [value, delay])

  return debounced
}
