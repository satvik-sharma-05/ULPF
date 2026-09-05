"""
chat.py - Ask the log graph questions from the terminal.

    python chat.py                                     # interactive REPL - LLM planner decides what to do
    python chat.py --ask "top 5 hosts by error count"
    python chat.py --ask "why did storage fail?" --route graphrag   # force one fixed strategy instead
    python chat.py --explain-route "how many errors yesterday?"     # cheap heuristic pre-check, not the planner
    python chat.py --show-schema                       # what the bot thinks the graph is

With no --route, the LLM planner (planner.py) decides which tool(s) to call -
vector search, fulltext search, graph traversal, path traversal, a temporal
window, text-to-Cypher - possibly several in sequence, and every answer prints
its full tool-call trace (plus the generated Cypher, if any) - so a wrong
answer can be traced to a bad tool choice or a bad query, not left as an
opaque black box. --route bypasses the planner for a single fixed strategy.

This pipeline only needs a populated Neo4j; the ingestion pipeline is not
imported and does not need to be present.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Answers are free-text from whatever LLM is configured, which routinely
# includes punctuation (curly quotes, non-breaking hyphens, ...) outside a
# Windows console's default codepage (cp1252) - without this, printing such
# an answer crashes the whole CLI instead of just looking slightly different.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('--ask', help="Ask one question and exit (otherwise starts a REPL)")
    ap.add_argument('--route', choices=['text2cypher', 'graphrag', 'hybrid'],
                    help="Force one fixed strategy instead of letting the planner decide")
    ap.add_argument('--explain-route', metavar='QUESTION',
                    help="Show the cheap heuristic router's guess (not what the planner would actually do)")
    ap.add_argument('--show-schema', action='store_true',
                    help="Print the schema introspected from Neo4j, then exit")
    ap.add_argument('--neo4j-uri', default=None, help="Override NEO4J_URI")
    ap.add_argument('--neo4j-user', default=None, help="Override NEO4J_USER")
    ap.add_argument('--neo4j-password', default=None, help="Override NEO4J_PASSWORD")
    ap.add_argument('--neo4j-database', default=None, help="Override NEO4J_DATABASE")
    ap.add_argument('--verbose', action='store_true', help="Show routing/retrieval debug logs")
    args = ap.parse_args()

    # Applied before config.py is first imported, since it reads os.environ once.
    for flag, env in (('neo4j_uri', 'NEO4J_URI'), ('neo4j_user', 'NEO4J_USER'),
                      ('neo4j_password', 'NEO4J_PASSWORD'),
                      ('neo4j_database', 'NEO4J_DATABASE')):
        value = getattr(args, flag, None)
        if value:
            os.environ[env] = value

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format='%(levelname)s %(name)s - %(message)s',
    )

    from chatbot import LogChatbot
    from classifier import QueryClassifier

    # Routing needs no database, so it can be inspected before anything loads.
    if args.explain_route:
        route = QueryClassifier().classify(args.explain_route)
        print(f"Question   : {args.explain_route}")
        print(f"Strategy   : {route.strategy}")
        print(f"Confidence : {route.confidence:.2f}")
        print(f"Reason     : {route.reason}")
        return

    bot = LogChatbot()
    try:
        if args.show_schema:
            print(bot.schema_summary())
            return

        if args.ask:
            print(bot.ask(args.ask, force_route=args.route).render())
            return

        print("Log graph chatbot. Ask a question, or Ctrl-C / 'exit' to quit.\n")
        while True:
            try:
                question = input("you > ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not question:
                continue
            if question.lower() in ('exit', 'quit', ':q'):
                break
            print()
            print(bot.ask(question, force_route=args.route).render())
            print()
    finally:
        bot.close()


if __name__ == '__main__':
    main()
