"""CLI — batch ingestion and query interface."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.core.config import settings

console = Console()


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 2:
        _print_usage()
        return

    command = sys.argv[1]

    if command == "ingest":
        if len(sys.argv) < 3:
            console.print("[red]Usage: globle-mind ingest <file_or_directory>[/red]")
            return
        asyncio.run(_ingest(sys.argv[2]))

    elif command == "query":
        if len(sys.argv) < 3:
            console.print("[red]Usage: globle-mind query 'your question here'[/red]")
            return
        question = " ".join(sys.argv[2:])
        asyncio.run(_query(question))

    elif command == "serve":
        _serve()

    elif command == "health":
        asyncio.run(_health())

    else:
        console.print(f"[red]Unknown command: {command}[/red]")
        _print_usage()


def _print_usage() -> None:
    console.print("\n[bold]GlobleMind[/bold] — Zero-Cost Enterprise RAG Pipeline\n")
    console.print("Commands:")
    console.print("  [cyan]ingest[/cyan]  <file_or_dir>  Ingest documents into the pipeline")
    console.print("  [cyan]query[/cyan]   <question>     Query ingested documents")
    console.print("  [cyan]serve[/cyan]                  Start the FastAPI web server")
    console.print("  [cyan]health[/cyan]                 Check provider availability")
    console.print()


async def _ingest(path_str: str) -> None:
    """Ingest a file or directory."""
    from src.pipeline.ingestion import IngestionPipeline

    settings.ensure_dirs()
    path = Path(path_str)

    if not path.exists():
        console.print(f"[red]Path not found: {path}[/red]")
        return

    pipeline = IngestionPipeline()

    if path.is_file():
        files = [path]
    else:
        files = [f for f in path.rglob("*") if f.is_file() and not f.name.startswith(".")]

    console.print(f"\n[bold]Ingesting {len(files)} file(s)...[/bold]\n")

    semaphore = asyncio.Semaphore(3)

    async def ingest_one(file_path: Path) -> None:
        async with semaphore:
            try:
                result = await pipeline.ingest(file_path)

                if getattr(result, "skipped", False):
                    console.print(f"  [cyan]⏭[/cyan] {file_path.name}: Already ingested (deduplicated)")
                    return

                status = "[green]✓[/green]" if result.total_chunks > 0 else "[yellow]⚠[/yellow]"
                console.print(
                    f"  {status} {file_path.name}: "
                    f"{result.total_chunks} chunks, "
                    f"{result.document_type}, "
                    f"{result.total_pages} pages"
                )

                if result.warnings:
                    for w in result.warnings:
                        console.print(f"    [yellow]Warning: {w}[/yellow]")

            except Exception as e:
                console.print(f"  [red]✗ {file_path.name}: {e}[/red]")

    await asyncio.gather(*[ingest_one(f) for f in files])

    console.print("\n[bold green]Ingestion complete.[/bold green]")


async def _query(question: str) -> None:
    """Query the RAG pipeline."""
    from src.pipeline.query import QueryPipeline

    pipeline = QueryPipeline()
    result = await pipeline.query(question)

    console.print(f"\n[bold]Question:[/bold] {result.query}\n")
    console.print(f"[bold]Answer:[/bold]\n{result.answer}\n")

    if result.citations:
        table = Table(title="Citations")
        table.add_column("Chunk ID")
        table.add_column("Source")
        table.add_column("Page")
        table.add_column("Score")

        for c in result.citations:
            table.add_row(c.chunk_id, Path(c.source_file).name, str(c.page_number), f"{c.relevance_score:.3f}")

        console.print(table)

    console.print(f"\n[dim]Model: {result.model_used} | Retrieved: {result.chunks_retrieved} | Reranked: {result.chunks_after_rerank}[/dim]")


async def _health() -> None:
    """Check provider availability."""
    from src.core.provider_client import ProviderRouter

    router = ProviderRouter()

    table = Table(title="Provider Status")
    table.add_column("Provider")
    table.add_column("Available")

    for name, provider in router._providers.items():
        status = "[green]✓ Yes[/green]" if provider.is_available else "[red]✗ No[/red]"
        table.add_row(name, status)

    console.print(table)


def _serve() -> None:
    """Start the FastAPI server."""
    import uvicorn

    console.print("\n[bold]Starting GlobleMind server...[/bold]\n")
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=True)


if __name__ == "__main__":
    main()
