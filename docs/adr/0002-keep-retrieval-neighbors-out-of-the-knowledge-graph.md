# Keep Retrieval Neighbors out of the durable knowledge graph

Vault PR treats only explicitly authored note relationships as Durable Knowledge
Links. Lexical and semantic Retrieval Neighbors may guide a Semantic Transaction,
but they are not persisted automatically because similarity is weaker than a
reviewed semantic relationship. This keeps graph connectivity auditable even
though it grows more slowly than an automatically generated top-k graph.
