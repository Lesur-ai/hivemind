# -*- coding: utf-8 -*-
"""P10-2 pure signed enrollment artifacts and T17 authority contract."""

from __future__ import annotations

import base64
import dataclasses
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live_mem.core.hivemind.models import (
    Member,
    MemberStatus,
    MembershipView,
    PeerScope,
)
from live_mem.mesh.artifacts import (
    ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN,
    INVITATION_SIGNATURE_DOMAIN,
    JOIN_CLAIM_SIGNATURE_DOMAIN,
    MESH_INVITATION_TTL_MILLISECONDS,
    MESH_TARGET_UNBOUND,
    MeshArtifactError,
    MeshArtifactKind,
    MeshEnrollmentApproval,
    MeshInvitation,
    MeshJoinClaim,
    SignedMeshArtifact,
    artifact_canonical_bytes,
    verify_approval_authority,
    verify_artifact_chain,
)
from live_mem.mesh.canonical import canonical_dumps, canonical_loads
from live_mem.mesh.identity import (
    MeshPrivateKey,
    decode_mesh_public_key,
    mesh_identity_fingerprint,
)
from live_mem.mesh.wire import MeshHttpOperation


def _private(label: str) -> MeshPrivateKey:
    seed = hashlib.sha256(("mesh-artifact-test:" + label).encode("ascii")).digest()
    return MeshPrivateKey(Ed25519PrivateKey.from_private_bytes(seed))


def _identity(label: str) -> tuple[MeshPrivateKey, str, str]:
    private = _private(label)
    public = private.public_key()
    return private, public, mesh_identity_fingerprint(public)


def _legacy_public(public_key: str) -> str:
    raw = decode_mesh_public_key(public_key)
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _models() -> tuple[
    MeshPrivateKey,
    MeshPrivateKey,
    MeshInvitation,
    MeshJoinClaim,
    MeshEnrollmentApproval,
]:
    source_private, source_public, source_fingerprint = _identity("source")
    target_private, target_public, target_fingerprint = _identity("target")
    invitation = MeshInvitation(
        protocol_version=1,
        kind=MeshArtifactKind.INVITATION,
        pair_id="pair_" + "1" * 32,
        space_id="alpha",
        source_public_key=source_public,
        source_fingerprint=source_fingerprint,
        target_binding=MESH_TARGET_UNBOUND,
        membership_epoch=7,
        issued_at_ms=1_000,
        expires_at_ms=1_000 + MESH_INVITATION_TTL_MILLISECONDS,
        nonce="nonce_" + "2" * 64,
        secret_digest="3" * 64,
    )
    signed_invitation = SignedMeshArtifact.sign(invitation, source_private)
    claim = MeshJoinClaim(
        protocol_version=1,
        kind=MeshArtifactKind.JOIN_CLAIM,
        pair_id=invitation.pair_id,
        space_id=invitation.space_id,
        source_public_key=source_public,
        source_fingerprint=source_fingerprint,
        target_public_key=target_public,
        target_fingerprint=target_fingerprint,
        membership_epoch=invitation.membership_epoch,
        issued_at_ms=1_100,
        nonce="nonce_" + "4" * 64,
        invitation_digest=signed_invitation.digest(),
        requested_scopes=("commit", "read"),
    )
    signed_claim = SignedMeshArtifact.sign(claim, target_private)
    approval = MeshEnrollmentApproval(
        protocol_version=1,
        kind=MeshArtifactKind.ENROLLMENT_APPROVAL,
        pair_id=invitation.pair_id,
        space_id=invitation.space_id,
        source_public_key=source_public,
        source_fingerprint=source_fingerprint,
        target_public_key=target_public,
        target_fingerprint=target_fingerprint,
        membership_epoch=invitation.membership_epoch,
        issued_at_ms=1_200,
        nonce="nonce_" + "5" * 64,
        invitation_digest=signed_invitation.digest(),
        join_claim_digest=signed_claim.digest(),
        granted_scopes=("read",),
    )
    return source_private, target_private, invitation, claim, approval


def _signed_triple() -> tuple[
    SignedMeshArtifact, SignedMeshArtifact, SignedMeshArtifact
]:
    source_private, target_private, invitation, claim, approval = _models()
    return (
        SignedMeshArtifact.sign(invitation, source_private),
        SignedMeshArtifact.sign(claim, target_private),
        SignedMeshArtifact.sign(approval, source_private),
    )


def _resign_chain(
    invitation: MeshInvitation,
    claim: MeshJoinClaim,
    approval: MeshEnrollmentApproval,
    *,
    bind_digests: bool = True,
) -> tuple[SignedMeshArtifact, SignedMeshArtifact, SignedMeshArtifact]:
    source_private, _, _ = _identity("source")
    target_private, _, _ = _identity("target")
    signed_invitation = SignedMeshArtifact.sign(invitation, source_private)
    if bind_digests:
        claim = dataclasses.replace(
            claim, invitation_digest=signed_invitation.digest()
        )
    signed_claim = SignedMeshArtifact.sign(claim, target_private)
    if bind_digests:
        approval = dataclasses.replace(
            approval,
            invitation_digest=signed_invitation.digest(),
            join_claim_digest=signed_claim.digest(),
        )
    return (
        signed_invitation,
        signed_claim,
        SignedMeshArtifact.sign(approval, source_private),
    )


def test_artifact_and_http_operation_vocabularies_are_disjoint_and_closed() -> None:
    assert {kind.value for kind in MeshArtifactKind} == {
        "invitation",
        "join_claim",
        "enrollment_approval",
    }
    assert not ({kind.value for kind in MeshArtifactKind} & {op.value for op in MeshHttpOperation})
    for artifact in (item.artifact for item in _signed_triple()):
        assert "op" not in artifact.as_dict()
        assert "operation" not in artifact.as_dict()


def test_artifact_shapes_keep_invitation_unbound_and_claims_concrete() -> None:
    invitation, claim, approval = (item.artifact for item in _signed_triple())
    invitation_value = invitation.as_dict()
    assert invitation_value["target_binding"] == MESH_TARGET_UNBOUND
    assert "target_public_key" not in invitation_value
    assert "target_fingerprint" not in invitation_value

    for value in (claim.as_dict(), approval.as_dict()):
        assert value["target_public_key"].startswith("ed25519-public:v1:")
        assert value["target_fingerprint"].startswith("hm1:")
        assert "target_binding" not in value


@pytest.mark.parametrize(
    "ttl_ms",
    (
        MESH_INVITATION_TTL_MILLISECONDS - 1,
        MESH_INVITATION_TTL_MILLISECONDS + 1,
    ),
)
def test_invitation_requires_exact_one_hour_ttl_for_models_and_wire(
    ttl_ms: int,
) -> None:
    _, _, invitation, _, _ = _models()
    with pytest.raises(MeshArtifactError) as caught:
        dataclasses.replace(
            invitation,
            expires_at_ms=invitation.issued_at_ms + ttl_ms,
        )
    assert caught.value.code == "invalid_expiry"

    signed = _signed_triple()[0]
    wrapper = canonical_loads(signed.canonical_bytes())
    assert type(wrapper) is dict
    artifact = wrapper["artifact"]
    assert type(artifact) is dict
    artifact["expires_at_ms"] = artifact["issued_at_ms"] + ttl_ms
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.from_bytes(canonical_dumps(wrapper))
    assert caught.value.code == "invalid_expiry"


def test_signed_artifact_known_vectors_round_trip_and_digest_exact_signed_bytes() -> None:
    invitation, claim, approval = _signed_triple()
    expected = {
        MeshArtifactKind.INVITATION: (
            "82ea23a26af827c6ec40796eb76e371d58f3e19432f92b800d551add7a4defc9",
            "DMDjMhG7fr6qcIUQ6NgtgarhfSU9PGUXRy_-VWIBIatHIoVjgpPbm0uyzfwWe2glZ0BvSSjdQC9MQcCOehtDBQ",
        ),
        MeshArtifactKind.JOIN_CLAIM: (
            "b293fe3c005ce5efb25245b33b199a26e0d5b72bf0e63f755be21d62298a690d",
            "7QSqTO4SvTW93fy46pUfp_RU8yFwK3zka3O1mk6NkBjadujfpLog97IueB7V4cYLZY1xQ7BhLwrB2o--9XcBAA",
        ),
        MeshArtifactKind.ENROLLMENT_APPROVAL: (
            "f490f739c73d5d7e0a751dab0fd5fe48947415e8c4c6b4a1615ebb118092ce47",
            "SPyOGcVe7Jwj91gxKGiYTJosTcRUpqmXmFkpVbwsov5HrHlxg--5Gv9QsC6sx0WRECLRmCvEVro1EVAxBNPdDQ",
        ),
    }
    for signed in (invitation, claim, approval):
        signed.verify()
        encoded = signed.canonical_bytes()
        parsed = SignedMeshArtifact.from_bytes(encoded)
        assert parsed == signed
        parsed.verify()
        digest, signature = expected[signed.artifact.kind]
        assert hashlib.sha256(encoded).hexdigest() == digest
        assert signed.digest() == digest
        assert base64.urlsafe_b64encode(signed.signature).decode("ascii").rstrip("=") == signature


def test_artifact_signer_roles_and_domains_are_exact() -> None:
    source_private, target_private, invitation, claim, approval = _models()
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.sign(invitation, target_private)
    assert caught.value.code == "signer_identity_mismatch"
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.sign(claim, source_private)
    assert caught.value.code == "signer_identity_mismatch"
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.sign(approval, target_private)
    assert caught.value.code == "signer_identity_mismatch"

    wrong_domain_signatures = (
        (
            invitation,
            source_private.sign(
                ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN
                + artifact_canonical_bytes(invitation)
            ),
        ),
        (
            claim,
            target_private.sign(
                INVITATION_SIGNATURE_DOMAIN + artifact_canonical_bytes(claim)
            ),
        ),
        (
            approval,
            source_private.sign(
                JOIN_CLAIM_SIGNATURE_DOMAIN + artifact_canonical_bytes(approval)
            ),
        ),
    )
    assert len(
        {
            INVITATION_SIGNATURE_DOMAIN,
            JOIN_CLAIM_SIGNATURE_DOMAIN,
            ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN,
        }
    ) == 3
    for artifact, signature in wrong_domain_signatures:
        with pytest.raises(MeshArtifactError) as caught:
            SignedMeshArtifact(artifact=artifact, signature=signature).verify()
        assert caught.value.code == "authentication_failed"


def test_signed_artifact_parser_rejects_unknown_fields_and_kind_shape_overlap() -> None:
    invitation, _, _ = _signed_triple()
    signature = base64.urlsafe_b64encode(invitation.signature).decode("ascii").rstrip("=")
    value = invitation.artifact.as_dict()
    value["target_public_key"] = _identity("target")[1]
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.from_bytes(
            canonical_dumps({"artifact": value, "signature": signature})
        )
    assert caught.value.code == "invalid_artifact_shape"

    value = invitation.artifact.as_dict()
    value["op"] = "pair.claim"
    with pytest.raises(MeshArtifactError) as caught:
        SignedMeshArtifact.from_bytes(
            canonical_dumps({"artifact": value, "signature": signature})
        )
    assert caught.value.code == "invalid_artifact_shape"


def test_artifacts_reject_legacy_keys_unbound_targets_self_pairing_and_bad_scopes() -> None:
    source_private, target_private, invitation, claim, approval = _models()
    del source_private, target_private
    with pytest.raises(MeshArtifactError):
        dataclasses.replace(
            invitation,
            source_public_key=_legacy_public(invitation.source_public_key),
        )
    with pytest.raises(MeshArtifactError):
        dataclasses.replace(
            claim,
            target_public_key=claim.source_public_key,
            target_fingerprint=claim.source_fingerprint,
        )
    with pytest.raises(MeshArtifactError):
        dataclasses.replace(
            approval,
            target_public_key=MESH_TARGET_UNBOUND,
            target_fingerprint=MESH_TARGET_UNBOUND,
        )
    for scopes in (
        (),
        ("commit",),
        ("read", "commit"),
        ("read", "read"),
        ("admin", "read"),
    ):
        with pytest.raises(MeshArtifactError):
            dataclasses.replace(claim, requested_scopes=scopes)


def test_artifact_signatures_cover_every_artifact_field() -> None:
    source_private, target_private, invitation, claim, approval = _models()
    signed_models = (
        (invitation, SignedMeshArtifact.sign(invitation, source_private)),
        (claim, SignedMeshArtifact.sign(claim, target_private)),
        (approval, SignedMeshArtifact.sign(approval, source_private)),
    )
    for artifact, signed in signed_models:
        value = artifact.as_dict()
        value["nonce"] = "nonce_" + "f" * 64
        wrapper = canonical_loads(signed.canonical_bytes())
        assert type(wrapper) is dict
        wrapper["artifact"] = value
        changed = SignedMeshArtifact.from_bytes(canonical_dumps(wrapper))
        with pytest.raises(MeshArtifactError) as caught:
            changed.verify()
        assert caught.value.code == "authentication_failed"


def test_complete_artifact_chain_verifies() -> None:
    invitation, claim, approval = _signed_triple()
    assert verify_artifact_chain(invitation, claim, approval) is None


def test_artifact_chain_rejects_each_digest_link_after_valid_resigning() -> None:
    source_private, target_private, invitation, claim, approval = _models()
    signed_invitation = SignedMeshArtifact.sign(invitation, source_private)
    signed_claim = SignedMeshArtifact.sign(claim, target_private)

    bad_claim = SignedMeshArtifact.sign(
        dataclasses.replace(claim, invitation_digest="a" * 64), target_private
    )
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(
            signed_invitation,
            bad_claim,
            SignedMeshArtifact.sign(approval, source_private),
        )
    assert caught.value.code == "artifact_chain_digest_mismatch"

    bad_approval_invitation = SignedMeshArtifact.sign(
        dataclasses.replace(approval, invitation_digest="b" * 64), source_private
    )
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(
            signed_invitation, signed_claim, bad_approval_invitation
        )
    assert caught.value.code == "artifact_chain_digest_mismatch"

    bad_approval_claim = SignedMeshArtifact.sign(
        dataclasses.replace(approval, join_claim_digest="c" * 64), source_private
    )
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(signed_invitation, signed_claim, bad_approval_claim)
    assert caught.value.code == "artifact_chain_digest_mismatch"


@pytest.mark.parametrize(
    "mutation",
    ["pair", "space", "epoch", "source", "target"],
)
def test_artifact_chain_rejects_every_identity_and_context_binding(
    mutation: str,
) -> None:
    _, _, invitation, claim, approval = _models()
    if mutation == "pair":
        approval = dataclasses.replace(approval, pair_id="pair_" + "9" * 32)
    elif mutation == "space":
        approval = dataclasses.replace(approval, space_id="beta")
    elif mutation == "epoch":
        approval = dataclasses.replace(approval, membership_epoch=8)
    elif mutation == "source":
        _, other_public, other_fingerprint = _identity("other-source")
        claim = dataclasses.replace(
            claim,
            source_public_key=other_public,
            source_fingerprint=other_fingerprint,
        )
    else:
        _, other_public, other_fingerprint = _identity("other-target")
        approval = dataclasses.replace(
            approval,
            target_public_key=other_public,
            target_fingerprint=other_fingerprint,
        )
    signed = _resign_chain(invitation, claim, approval)
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(*signed)
    assert caught.value.code == "artifact_chain_binding_mismatch"


def test_artifact_chain_rejects_scope_escalation_after_valid_resigning() -> None:
    _, _, invitation, claim, approval = _models()
    claim = dataclasses.replace(claim, requested_scopes=("read",))
    approval = dataclasses.replace(approval, granted_scopes=("commit", "read"))
    signed = _resign_chain(invitation, claim, approval)
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(*signed)
    assert caught.value.code == "artifact_chain_scope_escalation"


@pytest.mark.parametrize("case", ["claim_before", "approval_before", "at_expiry"])
def test_artifact_chain_rejects_temporal_reordering_and_expiry(case: str) -> None:
    _, _, invitation, claim, approval = _models()
    if case == "claim_before":
        claim = dataclasses.replace(claim, issued_at_ms=999)
    elif case == "approval_before":
        approval = dataclasses.replace(approval, issued_at_ms=1_099)
    else:
        approval = dataclasses.replace(
            approval, issued_at_ms=invitation.expires_at_ms
        )
    signed = _resign_chain(invitation, claim, approval)
    with pytest.raises(MeshArtifactError) as caught:
        verify_artifact_chain(*signed)
    assert caught.value.code == "artifact_chain_time_mismatch"


def _member(
    node_id: str,
    public_key: str,
    *,
    status: MemberStatus = MemberStatus.ACTIVE,
    scopes: list[str] | None = None,
) -> Member:
    return Member(
        node_id=node_id,
        public_key=public_key,
        status=status,
        scopes=scopes,
    )


def test_t17_approval_authority_uses_exact_applied_membership_without_mutation() -> None:
    _, _, approval = _signed_triple()
    assert type(approval.artifact) is MeshEnrollmentApproval
    source_legacy = _legacy_public(approval.artifact.source_public_key)
    membership = MembershipView(
        epoch=approval.artifact.membership_epoch,
        members=[
            _member(
                "source",
                source_legacy,
                scopes=[PeerScope.READ.value, PeerScope.COMMIT.value],
            )
        ],
    )
    before = membership.model_dump_json()

    assert (
        verify_approval_authority(
            approval.artifact,
            membership,
            enrollment_space_id=approval.artifact.space_id,
        )
        is None
    )
    assert membership.model_dump_json() == before

    # The canonical v1 public encoding is also compared by raw key material.
    membership.members[0].public_key = approval.artifact.source_public_key
    verify_approval_authority(
        approval.artifact,
        membership,
        enrollment_space_id=approval.artifact.space_id,
    )


def test_t17_legacy_full_scope_member_remains_commit_eligible() -> None:
    _, _, approval = _signed_triple()
    assert type(approval.artifact) is MeshEnrollmentApproval
    membership = MembershipView(
        epoch=approval.artifact.membership_epoch,
        members=[
            _member(
                "legacy-source",
                _legacy_public(approval.artifact.source_public_key),
                scopes=None,
            )
        ],
    )
    verify_approval_authority(
        approval.artifact,
        membership,
        enrollment_space_id=approval.artifact.space_id,
    )


def test_t17_rejects_ineligible_revoked_unknown_target_duplicate_and_corrupt_keys() -> None:
    _, _, approval = _signed_triple()
    assert type(approval.artifact) is MeshEnrollmentApproval
    source = _legacy_public(approval.artifact.source_public_key)
    target = _legacy_public(approval.artifact.target_public_key)
    _, unknown_public, _ = _identity("unknown-member")
    unknown = _legacy_public(unknown_public)
    scenarios: list[MembershipView] = [
        MembershipView(
            epoch=7,
            members=[_member("source", source, scopes=["read"])],
        ),
        MembershipView(
            epoch=7,
            members=[
                _member(
                    "source",
                    source,
                    status=MemberStatus.LEAVING,
                    scopes=["commit", "read"],
                )
            ],
        ),
        MembershipView(
            epoch=7,
            members=[
                _member(
                    "source",
                    source,
                    status=MemberStatus.EVICTED,
                    scopes=["commit", "read"],
                )
            ],
        ),
        MembershipView(
            epoch=7,
            members=[_member("unknown", unknown, scopes=["commit", "read"])],
        ),
        MembershipView(
            epoch=7,
            members=[_member("target", target, scopes=["commit", "read"])],
        ),
        MembershipView(
            epoch=7,
            members=[
                _member("source-1", source, scopes=["commit", "read"]),
                _member("source-2", source, scopes=["commit", "read"]),
            ],
        ),
        MembershipView(
            epoch=7,
            members=[
                _member("source", source, scopes=["commit", "read"]),
                _member("corrupt", "not-a-key", scopes=["commit", "read"]),
            ],
        ),
        MembershipView(
            epoch=8,
            members=[_member("source", source, scopes=["commit", "read"])],
        ),
    ]
    for membership in scenarios:
        with pytest.raises(MeshArtifactError) as caught:
            verify_approval_authority(
                approval.artifact,
                membership,
                enrollment_space_id=approval.artifact.space_id,
            )
        assert caught.value.code == "source_not_authorized"

    valid_membership = MembershipView(
        epoch=7,
        members=[_member("source", source, scopes=["commit", "read"])],
    )
    with pytest.raises(MeshArtifactError) as caught:
        verify_approval_authority(
            approval.artifact,
            valid_membership,
            enrollment_space_id="beta",
        )
    assert caught.value.code == "source_not_authorized"


def test_t17_accepts_only_exact_approval_and_membership_types() -> None:
    invitation, _, approval = _signed_triple()
    membership = MembershipView(
        epoch=7,
        members=[
            _member(
                "source",
                _legacy_public(approval.artifact.source_public_key),  # type: ignore[attr-defined]
                scopes=["commit", "read"],
            )
        ],
    )
    with pytest.raises(MeshArtifactError) as caught:
        verify_approval_authority(
            invitation.artifact,  # type: ignore[arg-type]
            membership,
            enrollment_space_id="alpha",
        )
    assert caught.value.code == "source_not_authorized"

    class MembershipLookalike:
        protocol_version = 1
        epoch = 7
        members = membership.members

    with pytest.raises(MeshArtifactError) as caught:
        verify_approval_authority(
            approval.artifact,  # type: ignore[arg-type]
            MembershipLookalike(),  # type: ignore[arg-type]
            enrollment_space_id="alpha",
        )
    assert caught.value.code == "source_not_authorized"
