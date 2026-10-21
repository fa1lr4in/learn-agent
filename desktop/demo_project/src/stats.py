def summarize(values):
    count = len(values)
    return {
        "count": count,
        "total": sum(values),
        "average": sum(values) / count if count else 0.0,
        "min": min(values) if count else None,
        "max": max(values) if count else None,
    }
