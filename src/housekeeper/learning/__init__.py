"""Local, interpretable active-learning assistance for review prioritization.

The model ranks and explains review candidates from prior user decisions. It may NOT approve
movement, override protection, or alter canonical roles — predictions are suggestions only and
never feed manifest approval. It stays disabled below a minimum training count.
"""
