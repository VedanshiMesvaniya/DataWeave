import asyncio
from src.pipeline.query import QueryPipeline

async def run_tests():
    pipeline = QueryPipeline()
    
    print("\n--- Test 1: What is the total revenue for the Gaming customer segment? ---")
    res1 = await pipeline.query("What is the total revenue for the Gaming customer segment?")
    print(f"Intent classified by presence of SQL chunks: {'live_sql_001' in res1.answer}")
    print("Answer:")
    print(res1.answer)
    
    print("\n--- Test 2: Which GPU model sold the most units? ---")
    res2 = await pipeline.query("Which GPU model sold the most units?")
    print(f"Intent classified by presence of SQL chunks: {'live_sql_001' in res2.answer}")
    print("Answer:")
    print(res2.answer)

if __name__ == "__main__":
    asyncio.run(run_tests())
