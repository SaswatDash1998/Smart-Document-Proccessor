"""
Performance Metrics Collection and Reporting
Implements Prometheus-style metrics endpoint
"""

import asyncpg
import psutil
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List
from collections import defaultdict

# In-memory metrics storage (lightweight, no external dependencies)
class MetricsCollector:
    """Collect and expose application metrics"""
    
    def __init__(self):
        self.counters = defaultdict(int)
        self.gauges = defaultdict(float)
        self.histograms = defaultdict(list)
        self.start_time = time.time()
    
    def increment_counter(self, name: str, value: int = 1, tags: Dict[str, str] = None):
        """Increment a counter metric"""
        key = self._make_key(name, tags)
        self.counters[key] += value
    
    def set_gauge(self, name: str, value: float, tags: Dict[str, str] = None):
        """Set a gauge metric"""
        key = self._make_key(name, tags)
        self.gauges[key] = value
    
    def record_histogram(self, name: str, value: float, tags: Dict[str, str] = None):
        """Record a histogram value"""
        key = self._make_key(name, tags)
        self.histograms[key].append(value)
        
        # Keep only last 1000 values to prevent memory issues
        if len(self.histograms[key]) > 1000:
            self.histograms[key] = self.histograms[key][-1000:]
    
    def _make_key(self, name: str, tags: Dict[str, str] = None) -> str:
        """Create metric key with tags"""
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}{{{tag_str}}}"
    
    def get_all_metrics(self) -> Dict[str, Any]:
        """Get all metrics in a structured format"""
        return {
            "counters": dict(self.counters),
            "gauges": dict(self.gauges),
            "histograms": {
                key: self._histogram_stats(values)
                for key, values in self.histograms.items()
            },
        }
    
    def _histogram_stats(self, values: List[float]) -> Dict[str, float]:
        """Calculate histogram statistics"""
        if not values:
            return {"count": 0, "sum": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}
        
        sorted_values = sorted(values)
        count = len(sorted_values)
        
        return {
            "count": count,
            "sum": sum(sorted_values),
            "min": sorted_values[0],
            "max": sorted_values[-1],
            "avg": sum(sorted_values) / count,
            "p50": self._percentile(sorted_values, 0.50),
            "p95": self._percentile(sorted_values, 0.95),
            "p99": self._percentile(sorted_values, 0.99),
        }
    
    def _percentile(self, sorted_values: List[float], p: float) -> float:
        """Calculate percentile from sorted list"""
        if not sorted_values:
            return 0
        k = (len(sorted_values) - 1) * p
        f = int(k)
        c = int(k) + 1
        if f == c:
            return sorted_values[f]
        return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)
    
    def to_prometheus_format(self) -> str:
        """Export metrics in Prometheus text format"""
        lines = []
        
        # Counters
        for key, value in self.counters.items():
            lines.append(f"# TYPE {key.split('{')[0]} counter")
            lines.append(f"{key} {value}")
        
        # Gauges
        for key, value in self.gauges.items():
            lines.append(f"# TYPE {key.split('{')[0]} gauge")
            lines.append(f"{key} {value}")
        
        # Histograms
        for key, values in self.histograms.items():
            metric_name = key.split('{')[0]
            stats = self._histogram_stats(values)
            
            lines.append(f"# TYPE {metric_name} histogram")
            lines.append(f"{key}_count {stats['count']}")
            lines.append(f"{key}_sum {stats['sum']}")
            lines.append(f"{key}_avg {stats['avg']}")
            lines.append(f"{key}_min {stats['min']}")
            lines.append(f"{key}_max {stats['max']}")
            
            # Quantiles
            for q in [0.5, 0.95, 0.99]:
                quantile_key = key.replace('}', f',quantile="{q}"}}') if '{' in key else f"{key}{{quantile=\"{q}\"}}"
                lines.append(f"{quantile_key} {stats[f'p{int(q*100)}']}")
        
        # System metrics
        lines.append(f"# TYPE process_uptime_seconds gauge")
        lines.append(f"process_uptime_seconds {time.time() - self.start_time}")
        
        return "\n".join(lines) + "\n"


# Global metrics collector instance
metrics_collector = MetricsCollector()


async def get_database_metrics(pool: asyncpg.Pool) -> Dict[str, Any]:
    """Collect database-related metrics with error handling"""
    # Default values in case of errors
    result = {
        "database": {
            "total_documents": 0,
            "pool_size": 0,
            "pool_free_connections": 0,
            "pool_used_connections": 0,
        },
        "processing": {
            "total_24h": 0,
            "successful_24h": 0,
            "failed_24h": 0,
            "avg_time_ms": 0.0,
            "last_processing": None,
        },
        "api": {
            "requests_1h": 0,
            "avg_response_time_ms": 0.0,
            "errors_1h": 0,
            "rate_limited_1h": 0,
        },
    }
    
    try:
        async with pool.acquire() as conn:
            # Document counts
            try:
                total_docs = await conn.fetchval("SELECT COUNT(*) FROM documents")
                result["database"]["total_documents"] = total_docs or 0
            except Exception as e:
                print(f"Warning: Could not fetch document count: {e}")
            
            # Processing history stats
            try:
                processing_stats = await conn.fetchrow("""
                    SELECT 
                        COUNT(*) as total_processed,
                        COUNT(*) FILTER (WHERE status = 'success') as successful,
                        COUNT(*) FILTER (WHERE status = 'failed') as failed,
                        AVG(processing_time_ms) FILTER (WHERE status = 'success') as avg_time_ms,
                        MAX(created_at) as last_processing
                    FROM processing_history
                    WHERE created_at > NOW() - INTERVAL '24 hours'
                """)
                
                if processing_stats:
                    result["processing"] = {
                        "total_24h": processing_stats["total_processed"] or 0,
                        "successful_24h": processing_stats["successful"] or 0,
                        "failed_24h": processing_stats["failed"] or 0,
                        "avg_time_ms": float(processing_stats["avg_time_ms"] or 0),
                        "last_processing": processing_stats["last_processing"].isoformat() if processing_stats["last_processing"] else None,
                    }
            except Exception as e:
                print(f"Warning: Could not fetch processing stats: {e}")
            
            # API request stats
            try:
                api_stats = await conn.fetchrow("""
                    SELECT 
                        COUNT(*) as total_requests,
                        AVG(response_time_ms) as avg_response_time,
                        COUNT(*) FILTER (WHERE status_code >= 400) as error_count,
                        COUNT(*) FILTER (WHERE status_code = 429) as rate_limited
                    FROM api_requests
                    WHERE created_at > NOW() - INTERVAL '1 hour'
                """)
                
                if api_stats:
                    result["api"] = {
                        "requests_1h": api_stats["total_requests"] or 0,
                        "avg_response_time_ms": float(api_stats["avg_response_time"] or 0),
                        "errors_1h": api_stats["error_count"] or 0,
                        "rate_limited_1h": api_stats["rate_limited"] or 0,
                    }
            except Exception as e:
                print(f"Warning: Could not fetch API stats: {e}")
            
            # Database connection pool stats
            try:
                pool_size = pool.get_size()
                pool_free = pool.get_idle_size()
                result["database"]["pool_size"] = pool_size
                result["database"]["pool_free_connections"] = pool_free
                result["database"]["pool_used_connections"] = pool_size - pool_free
            except Exception as e:
                print(f"Warning: Could not fetch pool stats: {e}")
                
    except Exception as e:
        print(f"Error in get_database_metrics: {e}")
    
    return result


def get_system_metrics() -> Dict[str, Any]:
    """Collect system resource metrics"""
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    return {
        "system": {
            "cpu_percent": cpu_percent,
            "memory_total_mb": memory.total / (1024 * 1024),
            "memory_used_mb": memory.used / (1024 * 1024),
            "memory_percent": memory.percent,
            "disk_total_gb": disk.total / (1024 * 1024 * 1024),
            "disk_used_gb": disk.used / (1024 * 1024 * 1024),
            "disk_percent": disk.percent,
        }
    }


async def get_graphrag_metrics() -> Dict[str, Any]:
    """Get GraphRAG index metrics (lightweight, no building) with error handling"""
    from pathlib import Path
    
    result = {
        "graphrag": {
            "index_exists": False,
            "input_files": 0,
            "entities_count": 0,
            "relationships_count": 0,
            "parquet_files": 0,
        }
    }
    
    try:
        data_dir = Path("data")
        output_dir = data_dir / "output"
        input_dir = data_dir / "input"
        
        index_exists = output_dir.exists() and any(output_dir.glob("*.parquet"))
        result["graphrag"]["index_exists"] = index_exists
        
        entities_count = 0
        relationships_count = 0
        
        if index_exists:
            try:
                import pandas as pd
                
                entities_file = output_dir / "entities.parquet"
                relationships_file = output_dir / "relationships.parquet"
                
                if entities_file.exists():
                    entities_df = pd.read_parquet(entities_file)
                    entities_count = len(entities_df)
                
                if relationships_file.exists():
                    relationships_df = pd.read_parquet(relationships_file)
                    relationships_count = len(relationships_df)
            except Exception as e:
                print(f"Warning: Could not read parquet files: {e}")
        
        result["graphrag"]["entities_count"] = entities_count
        result["graphrag"]["relationships_count"] = relationships_count
        result["graphrag"]["input_files"] = len(list(input_dir.glob("*.txt"))) if input_dir.exists() else 0
        result["graphrag"]["parquet_files"] = len(list(output_dir.glob("*.parquet"))) if output_dir.exists() else 0
        
    except Exception as e:
        print(f"Error in get_graphrag_metrics: {e}")
    
    return result


async def collect_all_metrics(pool: asyncpg.Pool) -> Dict[str, Any]:
    """Collect all application metrics"""
    db_metrics = await get_database_metrics(pool)
    system_metrics = get_system_metrics()
    graphrag_metrics = await get_graphrag_metrics()
    app_metrics = metrics_collector.get_all_metrics()
    
    # Update gauges for current state
    metrics_collector.set_gauge("documents_total", db_metrics["database"]["total_documents"])
    metrics_collector.set_gauge("db_pool_size", db_metrics["database"]["pool_size"])
    metrics_collector.set_gauge("cpu_percent", system_metrics["system"]["cpu_percent"])
    metrics_collector.set_gauge("memory_percent", system_metrics["system"]["memory_percent"])
    
    return {
        **db_metrics,
        **system_metrics,
        **graphrag_metrics,
        "application": {
            "uptime_seconds": time.time() - metrics_collector.start_time,
            "metrics": app_metrics,
        },
        "timestamp": datetime.utcnow().isoformat(),
    }


async def get_processing_history_stats(pool: asyncpg.Pool, days: int = 7) -> Dict[str, Any]:
    """Get detailed processing history statistics"""
    async with pool.acquire() as conn:
        # Daily stats
        daily_stats = await conn.fetch("""
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE status = 'success') as successful,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                AVG(processing_time_ms) FILTER (WHERE status = 'success') as avg_time,
                SUM(characters_extracted) as total_characters,
                SUM(chunks_created) as total_chunks
            FROM processing_history
            WHERE created_at > NOW() - INTERVAL '%s days'
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        """ % days)
        
        # Top file types
        file_types = await conn.fetch("""
            SELECT 
                file_type,
                COUNT(*) as count,
                AVG(processing_time_ms) as avg_time
            FROM processing_history
            WHERE created_at > NOW() - INTERVAL '%s days'
            GROUP BY file_type
            ORDER BY count DESC
        """ % days)
        
        return {
            "daily_stats": [dict(row) for row in daily_stats],
            "file_types": [dict(row) for row in file_types],
        }
