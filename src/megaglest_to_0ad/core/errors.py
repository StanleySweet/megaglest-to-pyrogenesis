"""Error hierarchy for the converter."""


class ConversionError(Exception):
    """Base class for all converter errors."""


class PackStructureError(ConversionError):
    """The input pack layout is missing or invalid."""


class ParseError(ConversionError):
    """A MegaGlest data file could not be parsed."""


class AssetReferenceError(ConversionError):
    """A referenced asset is missing or unresolvable."""

