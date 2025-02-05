from typing import Dict, List

from snowflake.ml._internal.utils.sql_identifier import (
    SqlIdentifier,
    to_sql_identifiers,
)

ENTITY_NAME_LENGTH_LIMIT = 32
FEATURE_VIEW_ENTITY_TAG_DELIMITER = ","
ENTITY_JOIN_KEY_DELIMITER = ","
# join key length limit is the length limit of TAG value
ENTITY_JOIN_KEY_LENGTH_LIMIT = 256


class Entity:
    """
    Entity encapsulates additional metadata for feature definition.
    Entity is typically used together with FeatureView to define join_keys and associate relevant FeatureViews.
    It can also be used for FeatureView search and lineage tracking.
    """

    def __init__(self, name: str, join_keys: List[str], desc: str = "") -> None:
        """
        Creates an Entity instance.

        Args:
            name: name of the Entity.
            join_keys: join keys associated with a FeatureView, used for feature retrieval.
            desc: description of the Entity.
        """
        self._validate(name, join_keys)

        self.name: SqlIdentifier = SqlIdentifier(name)
        self.join_keys: List[SqlIdentifier] = to_sql_identifiers(join_keys)
        self.desc = desc

    def _validate(self, name: str, join_keys: List[str]) -> None:
        if len(name) > ENTITY_NAME_LENGTH_LIMIT:
            raise ValueError(f"Entity name `{name}` exceeds maximum length: {ENTITY_NAME_LENGTH_LIMIT}")
        if FEATURE_VIEW_ENTITY_TAG_DELIMITER in name:
            raise ValueError(f"Entity name contains invalid char: `{FEATURE_VIEW_ENTITY_TAG_DELIMITER}`")
        if len(set(join_keys)) != len(join_keys):
            raise ValueError(f"Duplicate join keys detected in: {join_keys}")
        if len(FEATURE_VIEW_ENTITY_TAG_DELIMITER.join(join_keys)) > ENTITY_JOIN_KEY_LENGTH_LIMIT:
            raise ValueError(f"Total length of join keys exceeded maximum length: {ENTITY_JOIN_KEY_LENGTH_LIMIT}")

        for k in join_keys:
            if ENTITY_JOIN_KEY_DELIMITER in k:
                raise ValueError(f"Invalid char `{ENTITY_JOIN_KEY_DELIMITER}` detected in join key {k}")

    def _to_dict(self) -> Dict[str, str]:
        entity_dict = self.__dict__.copy()
        for k, v in entity_dict.items():
            if isinstance(v, SqlIdentifier):
                entity_dict[k] = str(v)
        return entity_dict

    def __repr__(self) -> str:
        states = (f"{k}={v}" for k, v in vars(self).items())
        return f"{type(self).__name__}({', '.join(states)})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Entity):
            return False

        return self.name == other.name and self.desc == other.desc and self.join_keys == other.join_keys
