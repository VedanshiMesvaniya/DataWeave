"""In-depth end-to-end integration tests for the full RAG Ingestion and Query Pipelines."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
import pytest
import pymupdf as fitz  # PyMuPDF

from src.core.config import settings
from src.core.provider_client import ProviderRouter
from src.models.schemas import FileCategory, PageStructure
from src.pipeline.ingestion import IngestionPipeline
from src.pipeline.query import QueryPipeline
from src.stages.s11_vector_store import QdrantStore

# Set up logging for the test run
logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def event_loop():
    """Create an instance of the default event loop for the test module."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest.mark.asyncio
async def test_full_pipeline_e2e_indepth() -> None:
    """Rigorous end-to-end integration test of the entire pipeline.
    
    1. Generates 3 types of source documents (Markdown, CSV, Native PDF).
    2. Ingests them via IngestionPipeline into an isolated Qdrant test collection.
    3. Runs queries targeting each document.
    4. Asserts correct retrieval, reranking, answering, and citation generation.
    5. Cleans up the test collection and files.
    """
    logger.info("Starting in-depth E2E RAG Pipeline Test")
    
    # 1. Force local in-memory Qdrant store for the test
    settings.qdrant_url = ""
    settings.qdrant_api_key = ""
    test_collection = "globle_mind_e2e_integration_test_collection"
    vector_store = QdrantStore(collection_name=test_collection)
    
    # Enforce cleanup in case of test failures
    try:
        # Initialize pipeline components using the isolated store
        router = ProviderRouter()
        
        # Verify providers are available
        available_providers = [name for name, p in router._providers.items() if p.is_available]
        logger.info("Available providers: %s", available_providers)
        assert len(available_providers) > 0, "No LLM/Vision providers are available for the test"
        
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            
            # Isolated registry for the test
            from src.core.ingestion_registry import IngestionRegistry
            test_registry = IngestionRegistry(registry_path=tmp_path / "test_registry.json")

            ingestion_pipeline = IngestionPipeline(
                router=router,
                vector_store=vector_store,
                registry=test_registry,
            )
        
            query_pipeline = QueryPipeline(
                router=router,
                vector_store=vector_store
            )
            
            # --- Document 1: Markdown ---
            md_file = tmp_path / "quantum_computing.md"
            md_content = (
                "# Quantum Computing Overview\n\n"
                "Quantum computing is a rapidly-emerging technology that harnesses the laws of quantum mechanics "
                "to solve problems too complex for classical computers.\n\n"
                "## Key Quantum Principles\n\n"
                "- **Superposition**: The ability of a quantum system to be in multiple states at the same time.\n"
                "- **Entanglement**: A phenomenon where quantum particles are linked, such that the state of one "
                "instantaneously influences the state of another, regardless of distance.\n\n"
                "## Superconducting Qubits\n\n"
                "Superconducting qubits operate at extremely low temperatures, close to absolute zero (approx. 0.015 Kelvin). "
                "Companies like IBM, Google, and Rigetti are leading the development in this space."
            )
            md_file.write_text(md_content)
            
            # --- Document 2: CSV (Table Data) ---
            csv_file = tmp_path / "financial_performance.csv"
            csv_content = (
                "Quarter,Revenue_Millions,Net_Income_Millions,YoY_Growth_Pct\n"
                "Q1_2026,120.5,15.2,12.5\n"
                "Q2_2026,135.2,18.4,14.8\n"
                "Q3_2026,142.0,22.1,16.2\n"
                "Q4_2026,165.8,31.4,21.0\n"
            )
            csv_file.write_text(csv_content)
            
            # --- Document 3: Native PDF ---
            pdf_file = tmp_path / "project_antigravity.pdf"
            doc = fitz.open()
            page = doc.new_page()
            
            # Draw native layout (Headings and prose)
            page.insert_text((50, 50), "Project Antigravity: Advanced RAG Architecture", fontsize=18)
            page.insert_text((50, 80), "Document ID: ANTIGRAVITY-SPEC-2026-X4", fontsize=10)
            
            page.insert_text((50, 120), "1. Executive Summary", fontsize=14)
            prose_1 = (
                "Project Antigravity introduces an accuracy-first, zero-cost RAG architecture designed "
                "to process heterogeneous enterprise documents. The pipeline leverages free-tier API endpoints "
                "with an advanced cross-provider fallback routing mechanism."
            )
            page.insert_text((50, 140), prose_1, fontsize=11)
            
            page.insert_text((50, 200), "2. Dual-Layer Layout Detection", fontsize=14)
            prose_2 = (
                "The layout detection stage (Stage 5) uses a structural layer for native PDFs based on "
                "PyMuPDF geometry to find footnote regions, headers, and column reading orders. For scanned "
                "pages, it falls back to a semantic layout layer utilizing a Vision-LLM prompt."
            )
            page.insert_text((50, 220), prose_2, fontsize=11)
            
            doc.save(str(pdf_file))
            doc.close()
            
            # 2. Ingest Documents
            logger.info("Ingesting Markdown file...")
            md_result = await ingestion_pipeline.ingest(md_file)
            assert md_result.total_chunks > 0
            assert md_result.file_category == FileCategory.MARKDOWN.value
            
            logger.info("Ingesting CSV file...")
            csv_result = await ingestion_pipeline.ingest(csv_file)
            assert csv_result.total_chunks > 0
            assert csv_result.file_category == FileCategory.CSV.value
            
            logger.info("Ingesting PDF file...")
            pdf_result = await ingestion_pipeline.ingest(pdf_file)
            assert pdf_result.total_chunks > 0
            assert pdf_result.file_category == FileCategory.PDF.value
            
            logger.info("All documents successfully ingested and indexed!")
            
            # Verify items are in store
            stats = await vector_store.get_stats()
            logger.info("Isolated Qdrant stats: %s", stats)
            assert stats["points_count"] > 0
            
            # 3. Test Retrieval, Reranking, and QA Generation
            
            # Query A: Target Markdown
            query_md = "What temperature do superconducting qubits operate at?"
            logger.info("Running query A: %s", query_md)
            result_md = await query_pipeline.query(query_md)
            
            logger.info("Answer A:\n%s", result_md.answer)
            assert result_md.chunks_retrieved > 0
            assert "0.015" in result_md.answer or "Kelvin" in result_md.answer
            assert len(result_md.citations) > 0
            assert "quantum_computing.md" in result_md.citations[0].source_file
            
            # Query B: Target CSV
            query_csv = "What was the YoY growth percentage in Q3 2026?"
            logger.info("Running query B: %s", query_csv)
            result_csv = await query_pipeline.query(query_csv)
            
            logger.info("Answer B:\n%s", result_csv.answer)
            assert result_csv.chunks_retrieved > 0
            assert "16.2" in result_csv.answer or "16.2%" in result_csv.answer
            assert len(result_csv.citations) > 0
            assert "financial_performance.csv" in result_csv.citations[0].source_file
            
            # Query C: Target Native PDF
            query_pdf = "Explain Stage 5 layout detection in Project Antigravity."
            logger.info("Running query C: %s", query_pdf)
            result_pdf = await query_pipeline.query(query_pdf)
            
            logger.info("Answer C:\n%s", result_pdf.answer)
            assert result_pdf.chunks_retrieved > 0
            assert "PyMuPDF" in result_pdf.answer or "geometry" in result_pdf.answer or "scanned" in result_pdf.answer
            assert len(result_pdf.citations) > 0
            assert "project_antigravity.pdf" in result_pdf.citations[0].source_file

    finally:
        # 4. Clean up the isolated Qdrant collection
        logger.info("Cleaning up isolated Qdrant test collection...")
        try:
            client = await vector_store._get_client()
            await client.delete_collection(test_collection)
            logger.info("Isolated Qdrant test collection successfully deleted.")
        except Exception as e:
            logger.error("Failed to clean up Qdrant collection: %s", e)
