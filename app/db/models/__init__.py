"""SQLAlchemy models mirroring prisma/schema.prisma exactly (Phase 1: no schema changes).

CustomerToken and CodeVerifier stay unmirrored -- they only ever back the dead customer-account-
OAuth/MCP cluster (app/auth.server.js, app/mcp-client.js), never imported by any live Node route,
per the Shopify-port migration audit. Session IS mirrored (read-only from Python's side): the
merchant's offline access token already lives there from the one real OAuth install Node already
did, and Python's Shopify Admin GraphQL calls just read it -- no OAuth flow reimplemented.

Prisma has no @@map directives in this schema, so table names are the exact PascalCase model
names below; most cross-model links are logical string matches, not enforced FKs, exactly as in
Prisma -- only Message->Conversation and the inventory snapshot/component chain declare real FKs.
"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "Conversation"

    id: Mapped[str] = mapped_column(primary_key=True)
    customerEmail: Mapped[str | None]
    customerName: Mapped[str | None]
    createdAt: Mapped[datetime]
    updatedAt: Mapped[datetime]

    messages: Mapped[list["Message"]] = relationship(back_populates="conversation")


class Message(Base):
    __tablename__ = "Message"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversationId: Mapped[str] = mapped_column(ForeignKey("Conversation.id", ondelete="CASCADE"))
    role: Mapped[str]
    content: Mapped[str]
    createdAt: Mapped[datetime]

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")


class Session(Base):
    __tablename__ = "Session"

    id: Mapped[str] = mapped_column(primary_key=True)
    shop: Mapped[str]
    state: Mapped[str]
    isOnline: Mapped[bool] = mapped_column(default=False)
    scope: Mapped[str | None]
    expires: Mapped[datetime | None]
    accessToken: Mapped[str]
    userId: Mapped[int | None]
    firstName: Mapped[str | None]
    lastName: Mapped[str | None]
    email: Mapped[str | None]
    accountOwner: Mapped[bool] = mapped_column(default=False)
    locale: Mapped[str | None]
    collaborator: Mapped[bool | None] = mapped_column(default=False)
    emailVerified: Mapped[bool | None] = mapped_column(default=False)


class CustomerAccountUrls(Base):
    __tablename__ = "CustomerAccountUrls"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversationId: Mapped[str] = mapped_column(unique=True)
    mcpApiUrl: Mapped[str | None]
    authorizationUrl: Mapped[str | None]
    tokenUrl: Mapped[str | None]
    createdAt: Mapped[datetime]
    updatedAt: Mapped[datetime]


class Note(Base):
    __tablename__ = "Note"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    position: Mapped[str]
    density: Mapped[float]
    family: Mapped[str | None]
    createdAt: Mapped[datetime]


class OrderHistory(Base):
    __tablename__ = "OrderHistory"

    id: Mapped[str] = mapped_column(primary_key=True)
    orderDate: Mapped[str | None]
    season: Mapped[str | None]
    classification: Mapped[str | None]
    notes: Mapped[str]
    city: Mapped[str | None] = mapped_column(index=True)
    stateName: Mapped[str | None] = mapped_column(index=True)
    countryName: Mapped[str | None] = mapped_column(index=True)
    productName: Mapped[str | None]
    normalizedProductName: Mapped[str | None] = mapped_column(index=True)
    # Salted HMAC of the source customer name -- never the name itself. See CUSTOMER_KEY_HASH_SALT.
    customerKeyHash: Mapped[str | None] = mapped_column(index=True)
    createdAt: Mapped[datetime]


class FragranceProduct(Base):
    __tablename__ = "FragranceProduct"

    id: Mapped[str] = mapped_column(primary_key=True)
    handle: Mapped[str | None]
    title: Mapped[str]
    normalizedTitle: Mapped[str] = mapped_column(unique=True)
    notesRaw: Mapped[str | None]
    notesJson: Mapped[list | None] = mapped_column(JSON)
    fragranceFamily: Mapped[str | None] = mapped_column(index=True)
    collection: Mapped[str | None] = mapped_column(index=True)
    pricePer5ml: Mapped[float | None]
    tagLine: Mapped[str | None]
    inspirationName: Mapped[str | None]
    inspirationBrand: Mapped[str | None]
    isSingleInspiration: Mapped[bool] = mapped_column(default=False)
    createdAt: Mapped[datetime]
    updatedAt: Mapped[datetime]


class OdooOilMapping(Base):
    __tablename__ = "OdooOilMapping"

    id: Mapped[str] = mapped_column(primary_key=True)
    fragranceProductId: Mapped[str] = mapped_column(unique=True)
    # Intentionally NOT unique -- multiple DUA products (different editions) can share one
    # physical Odoo oil SKU.
    odooSku: Mapped[str] = mapped_column(index=True)
    odooProductId: Mapped[int | None]
    odooVariantId: Mapped[int | None]
    odooName: Mapped[str | None]
    unitOfMeasure: Mapped[str | None]
    active: Mapped[bool] = mapped_column(default=True)
    lastVerifiedAt: Mapped[datetime | None]
    createdAt: Mapped[datetime]
    updatedAt: Mapped[datetime]


class ExistingCombination(Base):
    __tablename__ = "ExistingCombination"

    id: Mapped[str] = mapped_column(primary_key=True)
    title: Mapped[str]
    normalizedTitle: Mapped[str]
    type: Mapped[str] = mapped_column(index=True)  # HYBRID | TRIBRID | QUADBRID
    componentProductsJson: Mapped[list] = mapped_column(JSON)
    # Order-independent key -- see app/fragrance/combination_key.py port of combinationKey.js.
    componentKey: Mapped[str] = mapped_column(unique=True)
    tagLine: Mapped[str | None]
    createdAt: Mapped[datetime]


class ProductRegionSummary(Base):
    __tablename__ = "ProductRegionSummary"
    __table_args__ = (UniqueConstraint("normalizedProductName", "scope", "scopeValue"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    normalizedProductName: Mapped[str]
    scope: Mapped[str] = mapped_column(index=True)  # country | state | season | classification_global
    scopeValue: Mapped[str] = mapped_column(index=True)
    orderCount: Mapped[int]
    distinctCustomerCount: Mapped[int]
    repeatCustomerCount: Mapped[int]
    updatedAt: Mapped[datetime]


class CustomerProfileState(Base):
    __tablename__ = "CustomerProfileState"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversationId: Mapped[str] = mapped_column(unique=True, index=True)
    profileJson: Mapped[dict] = mapped_column(JSON)
    createdAt: Mapped[datetime]
    updatedAt: Mapped[datetime]


class FragranceRecommendation(Base):
    __tablename__ = "FragranceRecommendation"

    id: Mapped[str] = mapped_column(primary_key=True)
    conversationId: Mapped[str] = mapped_column(index=True)
    customerProfileJson: Mapped[dict] = mapped_column(JSON)
    # Real source product titles/notes/roles -- internal only, never read by customer-facing surfaces.
    productsJson: Mapped[dict] = mapped_column(JSON)
    combinationType: Mapped[str]
    scoreJson: Mapped[dict] = mapped_column(JSON)
    evidenceJson: Mapped[dict] = mapped_column(JSON)
    ratiosJson: Mapped[dict] = mapped_column(JSON)
    evidenceScope: Mapped[str | None]
    # The only fields any customer-facing surface may ever read from this record.
    customerFacingJson: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(default="pending", index=True)  # pending | confirmed | expired
    createdAt: Mapped[datetime]
    confirmedAt: Mapped[datetime | None]
    shopifyProductId: Mapped[str | None]
    buildStatus: Mapped[str] = mapped_column(default="draft")  # draft | saved
    shopifyVariantId: Mapped[str | None]
    draftName: Mapped[str | None]
    draftExcludedNotes: Mapped[list | None] = mapped_column(JSON)
    draftRatiosJson: Mapped[dict | None] = mapped_column(JSON)

    inventorySnapshot: Mapped["RecommendationInventorySnapshot | None"] = relationship(
        back_populates="recommendation"
    )


class RecommendationInventorySnapshot(Base):
    __tablename__ = "RecommendationInventorySnapshot"

    id: Mapped[str] = mapped_column(primary_key=True)
    recommendationId: Mapped[str] = mapped_column(
        ForeignKey("FragranceRecommendation.id", ondelete="CASCADE"), unique=True
    )
    inventoryValidated: Mapped[bool]
    buildable: Mapped[bool]
    checkedAt: Mapped[datetime]
    oilTotalMl: Mapped[float]
    alcoholMl: Mapped[float]
    maxBuildableBottles: Mapped[int | None]
    limitingSku: Mapped[str | None]
    # "ok" | "lookup_failed" -- the real HTTP/contract outcome, distinct from inventoryValidated.
    requestStatus: Mapped[str]
    createdAt: Mapped[datetime]

    recommendation: Mapped["FragranceRecommendation"] = relationship(
        back_populates="inventorySnapshot"
    )
    components: Mapped[list["RecommendationInventoryComponent"]] = relationship(
        back_populates="snapshot"
    )


class RecommendationInventoryComponent(Base):
    __tablename__ = "RecommendationInventoryComponent"

    id: Mapped[str] = mapped_column(primary_key=True)
    snapshotId: Mapped[str] = mapped_column(
        ForeignKey("RecommendationInventorySnapshot.id", ondelete="CASCADE"), index=True
    )
    # Nullable -- a component that never matched a real catalog row still gets a row here.
    fragranceProductId: Mapped[str | None]
    productTitle: Mapped[str]
    odooSku: Mapped[str | None]
    ratioPercent: Mapped[float]
    requiredOilMl: Mapped[float]
    onHandQty: Mapped[float | None]
    # MISSING | CONNECTED | SKU_NOT_FOUND | LOOKUP_FAILED -- mirrors odooInventory.server.js exactly.
    mappingStatus: Mapped[str]
    # null when unknown -- never a false "insufficient".
    sufficient: Mapped[bool | None]
    maxBuildableBottlesForComponent: Mapped[int | None]

    snapshot: Mapped["RecommendationInventorySnapshot"] = relationship(back_populates="components")


class BuildCapability(Base):
    """Phase 1 (security): a server-minted capability authorizing preview reads and Shopify build
    mutations for exactly one recommendation. Python-owned table (migrations/0001_build_capability.sql),
    not part of the shared Prisma schema. Only the token's SHA-256 hash is stored."""

    __tablename__ = "BuildCapability"

    id: Mapped[str] = mapped_column(primary_key=True)
    recommendationId: Mapped[str] = mapped_column(
        ForeignKey("FragranceRecommendation.id", ondelete="CASCADE"), index=True
    )
    conversationId: Mapped[str]
    shop: Mapped[str]
    tokenHash: Mapped[str] = mapped_column(unique=True)
    expiresAt: Mapped[datetime]
    revokedAt: Mapped[datetime | None]
    createdAt: Mapped[datetime]
