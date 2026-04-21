from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from db.core import db_connect, now_tz
from services.audit import audit
from services.parking import normalize_plate, plate_is_valid


# -----------------------------
# Helpers
# -----------------------------
def _digits(s: str | None) -> str:
    return re.sub(r"\D+", "", s or "")


def _mask_phone(s: str | None) -> str:
    d = _digits(s)
    if not d:
        return "-"
    if len(d) <= 4:
        return "*" * len(d)
    return ("*" * (len(d) - 4)) + d[-4:]


def _get_towers(con):
    return con.execute(
        "SELECT id, tower_num, etapa_residencial FROM towers WHERE is_active=1 ORDER BY tower_num"
    ).fetchall()


def _find_apartment(con, tower_num: int, apt_number: str) -> dict[str, Any] | None:
    row = con.execute(
        """
        SELECT a.*, t.tower_num, t.etapa_residencial
        FROM apartments a
        JOIN towers t ON t.id=a.tower_id
        WHERE t.tower_num=? AND a.apt_number=?
        LIMIT 1
        """,
        (int(tower_num), str(apt_number).strip()),
    ).fetchone()
    return dict(row) if row else None


def _upsert_apartment(
    con,
    *,
    tower_id: int,
    apt_number: str,
    resident_name: str | None,
    whatsapp: str | None,
    is_rented: int,
    owner_name: str | None,
    owner_whatsapp: str | None,
    tenant_name: str | None,
    tenant_whatsapp: str | None,
    has_private_parking: int,
    notes: str | None,
    is_active: int,
) -> tuple[str, int]:
    """Returns (mode, apt_id) where mode is created|updated"""
    try:
        con.execute(
            """
            INSERT INTO apartments(
                tower_id, apt_number,
                resident_name, whatsapp,
                has_private_parking, notes, is_active,
                is_rented, owner_name, owner_whatsapp,
                tenant_name, tenant_whatsapp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(tower_id),
                str(apt_number).strip(),
                resident_name,
                whatsapp,
                int(has_private_parking),
                notes,
                int(is_active),
                int(is_rented),
                owner_name,
                owner_whatsapp,
                tenant_name,
                tenant_whatsapp,
            ),
        )
        apt_id = int(con.execute("SELECT last_insert_rowid() as id").fetchone()["id"])
        return "created", apt_id
    except Exception:
        con.execute(
            """
            UPDATE apartments
            SET resident_name=?, whatsapp=?,
                has_private_parking=?, notes=?, is_active=?,
                is_rented=?, owner_name=?, owner_whatsapp=?,
                tenant_name=?, tenant_whatsapp=?
            WHERE tower_id=? AND apt_number=?
            """,
            (
                resident_name,
                whatsapp,
                int(has_private_parking),
                notes,
                int(is_active),
                int(is_rented),
                owner_name,
                owner_whatsapp,
                tenant_name,
                tenant_whatsapp,
                int(tower_id),
                str(apt_number).strip(),
            ),
        )
        row = con.execute(
            "SELECT id FROM apartments WHERE tower_id=? AND apt_number=? LIMIT 1",
            (int(tower_id), str(apt_number).strip()),
        ).fetchone()
        apt_id = int(row["id"]) if row else 0
        return "updated", apt_id


def _upsert_vehicle(
    con,
    *,
    apt_id: int,
    plate: str,
    vtype: str,
    brand: str | None,
    color: str | None,
    pmr: int,
    is_active: int,
) -> str:
    try:
        con.execute(
            """
            INSERT INTO resident_vehicles(plate,vehicle_type,apt_id,brand,color,is_pmr_authorized,is_active,created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (plate, vtype, int(apt_id), brand, color, int(pmr), int(is_active), now_tz().isoformat()),
        )
        return "created"
    except Exception:
        con.execute(
            """
            UPDATE resident_vehicles
            SET vehicle_type=?, apt_id=?, brand=?, color=?, is_pmr_authorized=?, is_active=?
            WHERE plate=?
            """,
            (vtype, int(apt_id), brand, color, int(pmr), int(is_active), plate),
        )
        return "updated"


# -----------------------------
# Public page
# -----------------------------
def page_resident_intake():
    """Formulario público para que residentes registren/actualicen datos del apto + vehículos."""
    st.subheader("📝 Registro de apartamento y vehículos (Primitiva Parque Natural)")
    st.caption(
        "Este formulario alimenta el sistema de parqueaderos. "
        "Si el apartamento ya tiene información registrada, el sistema lo notificará."
    )

    con = db_connect()

    towers = _get_towers(con)
    if not towers:
        st.warning("Aún no hay torres creadas en el sistema. Por favor intenta más tarde.")
        con.close()
        return

    # Mensaje persistente (no se pierde con rerun)
    last = st.session_state.get("_INTAKE_last_result")
    if last:
        with st.container(border=True):
            st.success(last.get("msg", "Listo ✅"))
            if last.get("details"):
                st.caption(last["details"])
            if st.button("Ocultar mensaje", key="INTAKE_hide_last"):
                st.session_state.pop("_INTAKE_last_result", None)
                st.rerun()

    # -------------------------
    # 1) Identificación del apartamento
    # -------------------------
    st.markdown("### 1) Identificación del apartamento")
    col1, col2 = st.columns([1, 1])
    with col1:
        tower_num = st.selectbox(
            "Torre",
            [int(t["tower_num"]) for t in towers],
            format_func=lambda n: f"Torre {n}",
            key="INTAKE_tower_num",
        )
    with col2:
        apt_number = st.text_input("Apartamento", placeholder="922", key="INTAKE_apt_number")

    apt_number = (apt_number or "").strip()

    existing = _find_apartment(con, int(tower_num), apt_number) if apt_number else None

    already_filled = False
    if existing:
        already_filled = bool(
            (existing.get("resident_name") or "").strip()
            or (existing.get("whatsapp") or "").strip()
            or (existing.get("owner_whatsapp") or "").strip()
            or (existing.get("tenant_whatsapp") or "").strip()
        )

    allow_update = True
    if existing and already_filled:
        with st.container(border=True):
            st.info(
                "Este apartamento **ya tiene información registrada**. "
                "Para actualizarla, debes verificar un dato de contacto."
            )
            st.caption(
                f"Contacto actual (enmascarado): {existing.get('resident_name') or '-'} · "
                f"WA: {_mask_phone(existing.get('whatsapp'))} · "
                f"Propietario WA: {_mask_phone(existing.get('owner_whatsapp'))}"
            )

            v = st.text_input(
                "Verificación: escribe los últimos 4 dígitos del WhatsApp registrado (propietario o residente)",
                key="INTAKE_verify_last4",
                max_chars=8,
            ).strip()

            last4 = re.sub(r"\D+", "", v)[-4:] if v else ""
            stored_last4 = ""
            for ph in [existing.get("owner_whatsapp"), existing.get("whatsapp"), existing.get("tenant_whatsapp")]:
                d = _digits(ph)
                if len(d) >= 4:
                    stored_last4 = d[-4:]
                    if last4 and last4 == stored_last4:
                        break

            allow_update = bool(last4 and stored_last4 and last4 == stored_last4)
            if not allow_update:
                st.warning("Para actualizar, necesitas digitar correctamente los últimos 4 dígitos del WhatsApp registrado.")

    tower_row = next(t for t in towers if int(t["tower_num"]) == int(tower_num))
    tower_id = int(tower_row["id"])

    # -------------------------
    # 2) Datos de contacto
    # -------------------------
    st.markdown("### 2) Datos de contacto")

    is_rented = st.checkbox(
        "El apartamento está arrendado",
        value=bool(existing.get("is_rented", 0)) if existing else False,
        key="INTAKE_is_rented",
    )

    filler = st.radio(
        "¿Quién diligencia este formulario?",
        ["Propietario", "Inquilino"],
        horizontal=True,
        key="INTAKE_filler",
    )

    default_owner_name = (existing.get("owner_name") if existing else "") or (existing.get("resident_name") if existing else "") or ""
    default_owner_wa = (existing.get("owner_whatsapp") if existing else "") or (existing.get("whatsapp") if existing else "") or ""
    default_tenant_name = (existing.get("tenant_name") if existing else "") or (existing.get("resident_name") if existing else "") or ""
    default_tenant_wa = (existing.get("tenant_whatsapp") if existing else "") or (existing.get("whatsapp") if existing else "") or ""
    default_res_name = (existing.get("resident_name") if existing else "") or ""
    default_res_wa = (existing.get("whatsapp") if existing else "") or ""

    has_private = st.checkbox(
        "Tiene parqueadero privado (celda privada)",
        value=bool(existing.get("has_private_parking", 0)) if existing else False,
        key="INTAKE_has_private",
    )
    notes = st.text_input("Notas (opcional)", value=(existing.get("notes") if existing else "") or "", key="INTAKE_notes")

    if not is_rented:
        st.caption("Si NO está arrendado: el contacto del sistema será el propietario (quien vive).")
        resident_name = st.text_input("Nombre", value=default_res_name or default_owner_name, key="INTAKE_resident_name")
        whatsapp = st.text_input("WhatsApp", value=default_res_wa or default_owner_wa, placeholder="3001234567", key="INTAKE_whatsapp")

        owner_name = resident_name
        owner_whatsapp = whatsapp
        tenant_name = ""
        tenant_whatsapp = ""
    else:
        st.caption("Si está arrendado: el contacto del sistema será el **inquilino** (quien vive).")
        colA, colB = st.columns(2)
        with colA:
            owner_name = st.text_input("Nombre del propietario", value=default_owner_name, key="INTAKE_owner_name")
            owner_whatsapp = st.text_input("WhatsApp del propietario", value=default_owner_wa, placeholder="3001234567", key="INTAKE_owner_wa")
        with colB:
            tenant_name = st.text_input("Nombre del inquilino", value=default_tenant_name, key="INTAKE_tenant_name")
            tenant_whatsapp = st.text_input("WhatsApp del inquilino", value=default_tenant_wa, placeholder="3001234567", key="INTAKE_tenant_wa")

        resident_name = tenant_name
        whatsapp = tenant_whatsapp

        if filler == "Inquilino":
            st.info("Como inquilino, debes registrar también los datos del propietario (obligatorio).")

    # -------------------------
    # 3) Vehículos
    # -------------------------
    st.markdown("### 3) Vehículos del apartamento")
    st.caption("Registra solo los vehículos de residentes (carros/motos) que usan el parqueadero.")

    nveh = int(st.number_input("¿Cuántos vehículos vas a registrar?", 0, 6, 0, step=1, key="INTAKE_nveh"))

    vehicles: list[dict[str, Any]] = []
    for i in range(nveh):
        with st.container(border=True):
            st.markdown(f"**Vehículo {i+1}**")
            c1, c2 = st.columns([1.2, 1])
            with c1:
                plate = st.text_input("Placa", key=f"INTAKE_plate_{i}", placeholder="ABC123")
            with c2:
                vtype = st.selectbox(
                    "Tipo",
                    ["CAR", "MOTO"],
                    key=f"INTAKE_vtype_{i}",
                    format_func=lambda x: "Carro" if x == "CAR" else "Moto",
                )
            c3, c4, c5 = st.columns([1, 1, 1])
            with c3:
                brand = st.text_input("Marca (opcional)", key=f"INTAKE_brand_{i}")
            with c4:
                color = st.text_input("Color (opcional)", key=f"INTAKE_color_{i}")
            with c5:
                pmr = st.checkbox("Autorizado PMR", value=False, key=f"INTAKE_pmr_{i}")

            vehicles.append(
                {
                    "plate": plate,
                    "vehicle_type": vtype,
                    "brand": brand,
                    "color": color,
                    "is_pmr_authorized": 1 if pmr else 0,
                    "is_active": 1,
                }
            )

    deactivate_missing = st.checkbox(
        "Desactivar vehículos anteriores del apartamento que NO estén en este formulario",
        value=False,
        key="INTAKE_deactivate_missing",
    )

    st.markdown("---")
    consent = st.checkbox(
        "Autorizo el uso interno de estos datos para gestión de parqueaderos y comunicaciones de la copropiedad.",
        value=False,
        key="INTAKE_consent",
    )

    submit_disabled = (not apt_number) or (existing and already_filled and not allow_update) or (not consent)

    btn = st.button(
        "✅ Guardar información",
        type="primary",
        disabled=submit_disabled,
        help=(
            "Completa Torre/Apartamento" if not apt_number else
            "Verifica el WhatsApp para actualizar" if (existing and already_filled and not allow_update) else
            "Debes autorizar el uso de datos" if not consent else
            None
        ),
        key="INTAKE_submit",
    )

    if btn:
        errors: list[str] = []

        if not apt_number:
            errors.append("Apartamento es obligatorio.")

        if not is_rented:
            if not (resident_name or "").strip():
                errors.append("Nombre es obligatorio.")
            if not _digits(whatsapp):
                errors.append("WhatsApp es obligatorio.")
        else:
            if not (owner_name or "").strip():
                errors.append("Nombre del propietario es obligatorio.")
            if not _digits(owner_whatsapp):
                errors.append("WhatsApp del propietario es obligatorio.")
            if not (tenant_name or "").strip():
                errors.append("Nombre del inquilino es obligatorio.")
            if not _digits(tenant_whatsapp):
                errors.append("WhatsApp del inquilino es obligatorio.")

        seen = set()
        norm_plates: list[str] = []
        for v in vehicles:
            p_raw = (v.get("plate") or "").strip()
            if not p_raw:
                errors.append("Hay un vehículo sin placa.")
                continue
            p = normalize_plate(p_raw)
            if not plate_is_valid(p):
                errors.append(f"Placa inválida: {p_raw}")
                continue
            if p in seen:
                errors.append(f"Placa repetida: {p}")
                continue
            seen.add(p)
            norm_plates.append(p)
            v["plate"] = p

        if errors:
            for e in errors:
                st.error(e)
            con.close()
            return

        mode = ""
        apt_id = 0
        v_created = 0
        v_updated = 0

        try:
            mode, apt_id = _upsert_apartment(
                con,
                tower_id=tower_id,
                apt_number=apt_number,
                resident_name=(resident_name or "").strip() or None,
                whatsapp=_digits(whatsapp) or None,
                is_rented=1 if is_rented else 0,
                owner_name=(owner_name or "").strip() or None,
                owner_whatsapp=_digits(owner_whatsapp) or None,
                tenant_name=(tenant_name or "").strip() or None,
                tenant_whatsapp=_digits(tenant_whatsapp) or None,
                has_private_parking=1 if has_private else 0,
                notes=(notes or "").strip() or None,
                is_active=1,
            )

            for v in vehicles:
                res = _upsert_vehicle(
                    con,
                    apt_id=apt_id,
                    plate=v["plate"],
                    vtype=v.get("vehicle_type") or "CAR",
                    brand=(v.get("brand") or "").strip() or None,
                    color=(v.get("color") or "").strip() or None,
                    pmr=int(v.get("is_pmr_authorized") or 0),
                    is_active=1,
                )
                if res == "created":
                    v_created += 1
                else:
                    v_updated += 1

            if deactivate_missing and apt_id:
                if norm_plates:
                    q_marks = ",".join(["?"] * len(norm_plates))
                    con.execute(
                        f"UPDATE resident_vehicles SET is_active=0 WHERE apt_id=? AND plate NOT IN ({q_marks})",
                        (int(apt_id), *norm_plates),
                    )
                else:
                    con.execute("UPDATE resident_vehicles SET is_active=0 WHERE apt_id=?", (int(apt_id),))

            con.commit()

            audit(
                "PUBLIC_INTAKE_SUBMIT",
                None,
                {
                    "tower_num": int(tower_num),
                    "apt_number": apt_number,
                    "mode": mode,
                    "is_rented": 1 if is_rented else 0,
                    "vehicles_created": v_created,
                    "vehicles_updated": v_updated,
                    "vehicles_submitted": len(vehicles),
                },
            )

            st.session_state["_INTAKE_last_result"] = {
                "msg": f"Guardado ✅ ({'creado' if mode=='created' else 'actualizado'}) · T{tower_num}-{apt_number}",
                "details": f"Vehículos: {len(vehicles)} (nuevos: {v_created}, actualizados: {v_updated})",
                "ts": datetime.utcnow().isoformat(),
            }
            st.rerun()

        except Exception as e:
            try:
                con.rollback()
            except Exception:
                pass
            st.error("No se pudo guardar la información.")
            st.exception(e)

    con.close()