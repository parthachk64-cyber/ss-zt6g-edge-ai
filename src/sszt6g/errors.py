"""Exception hierarchy. Every failure the system can reach has a named type."""


class SszError(Exception):
    """Base class for all companion errors."""


class ContractError(SszError):
    """A wire message violates its versioned contract."""


class EmptyHistoryError(SszError):
    """A forecast was requested with no telemetry history before the cutoff."""


class DataError(SszError):
    """Input data violates the import contract."""


class CapacityError(SszError):
    """A simulated capacity change cannot be honoured."""


class LedgerError(SszError):
    """The audit ledger could not record an entry."""


class LedgerUnavailable(LedgerError):
    """The ledger refused to accept an entry (outage)."""


class QuorumNotReached(LedgerError):
    """Fewer validators than the quorum voted for a block."""


class CrashError(SszError):
    """A simulated process crash injected for recovery tests."""


class BenchmarkAdapterError(SszError):
    """A benchmark-only adapter was used outside a declared benchmark."""
