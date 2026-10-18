def summarize(values):
    count = len(values)
    total = sum(values)
    return {
        "count": count,
        "total": total,
        "average": total / count if count else 0.0,
    }
