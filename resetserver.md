 1. Kill the running server                                                                                                                                                                                   
  pkill -f "python main.py" 2>/dev/null; pkill -f "uvicorn" 2>/dev/null                                                                                                                                        
                                                                                                                                                                                                               
  2. Flush Redis cache (LLM + TTS + RAG responses)                                                                                                                                                             
  redis-cli FLUSHDB                                                                                                                                                                                            
                                                                                                                                                                                                               
  3. Clear ChromaDB vector store (RAG embeddings — only if you want to re-ingest)
  rm -rf chroma_db/                                                                                                                                                                                            
  ▎ Skip this if your knowledge base hasn't changed — re-ingestion takes time.
                                                                                                                                                                                                               
  4. Clear SQLite databases (call history + audit log)                                                                                                                                                         
  rm -f data/calls.db data/audit.db                                                                                                                                                                            
                                                                                                                                                                                                               
  5. Clear Python bytecode cache                                                                                                                                                                               
  find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; find . -name "*.pyc" -delete 2>/dev/null                                                                                                     
   
  6. Start fresh                                                                                                                                                                                               
  cd /Users/ankitpanwar/Documents/github/python_aivoice_agent
 
 source .venv/bin/activate                                                                                                                                                                                    
 python main.py      