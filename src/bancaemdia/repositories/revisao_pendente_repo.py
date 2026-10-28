from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia import models
from bancaemdia.domain.registros import EstatisticasRevisao, FotoRevisao, RevisaoPendente
from bancaemdia.repositories.base import colunas


class RevisaoPendenteRepo:
    async def create(self, session: AsyncSession, dados: dict[str, object]) -> RevisaoPendente:
        obj = (
            await session.execute(
                insert(models.RevisaoPendente).values(**dados).returning(models.RevisaoPendente)
            )
        ).scalar_one()
        return RevisaoPendente(**colunas(obj))

    async def get_by_id(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> RevisaoPendente | None:
        stmt = select(models.RevisaoPendente).where(
            models.RevisaoPendente.usuario_id == usuario_id,
            models.RevisaoPendente.id == id_,
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else RevisaoPendente(**colunas(obj))

    async def get_by_id_for_update(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> RevisaoPendente | None:
        stmt = (
            select(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.id == id_,
            )
            .with_for_update()
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else RevisaoPendente(**colunas(obj))

    async def list_page(
        self,
        session: AsyncSession,
        usuario_id: int,
        filtros: dict[str, object],
        pagina: int = 1,
        tamanho: int = 50,
    ) -> tuple[list[RevisaoPendente], int]:
        stmt = select(
            models.RevisaoPendente,
            func.count().over().label("total"),
        ).where(
            models.RevisaoPendente.usuario_id == usuario_id,
            models.RevisaoPendente.resolvido_em.is_(None),
        )
        if filtros.get("motivo") is not None:
            stmt = stmt.where(models.RevisaoPendente.motivo == filtros["motivo"])
        if filtros.get("desde") is not None:
            stmt = stmt.where(models.RevisaoPendente.criado_em >= filtros["desde"])
        if filtros.get("ate") is not None:
            stmt = stmt.where(models.RevisaoPendente.criado_em < filtros["ate"])
        stmt = (
            stmt
            .order_by(models.RevisaoPendente.criado_em, models.RevisaoPendente.id)
            .limit(tamanho)
            .offset((pagina - 1) * tamanho)
        )
        linhas = (await session.execute(stmt)).all()
        if linhas:
            return (
                [RevisaoPendente(**colunas(linha[0])) for linha in linhas],
                int(linhas[0].total),
            )
        if pagina == 1:
            return [], 0
        contagem = select(func.count()).select_from(
            stmt.limit(None).offset(None).order_by(None).subquery()
        )
        return [], int((await session.execute(contagem)).scalar_one())

    async def stats(self, session: AsyncSession, usuario_id: int) -> EstatisticasRevisao:
        quantidade = func.count().label("quantidade")
        mais_antiga = func.min(models.RevisaoPendente.criado_em).label("mais_antiga_em")
        idade = func.greatest(0, func.extract("epoch", func.now() - mais_antiga)).label(
            "idade_maxima_segundos"
        )
        stmt = (
            select(models.RevisaoPendente.motivo, quantidade, mais_antiga, idade)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
            .group_by(models.RevisaoPendente.motivo)
            .order_by(models.RevisaoPendente.motivo)
        )
        linhas = (await session.execute(stmt)).all()
        if not linhas:
            return EstatisticasRevisao(0, {}, None, 0)
        por_motivo = {str(linha.motivo): int(linha.quantidade) for linha in linhas}
        antigas = [linha.mais_antiga_em for linha in linhas]
        # A réplica pode ter um pequeno desvio de relógio; idade nunca é uma duração negativa.
        idades = [max(0, int(linha.idade_maxima_segundos)) for linha in linhas]
        return EstatisticasRevisao(
            total=sum(por_motivo.values()),
            por_motivo=por_motivo,
            mais_antiga_em=min(antigas),
            idade_maxima_segundos=max(idades),
        )

    async def get_foto_by_id(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> FotoRevisao | None:
        revision = await self.get_by_id(session, usuario_id, id_)
        if (
            revision is not None
            and revision.resolvido_em is None
            and (
                (revision.extracao_bruta or {}).get("web_print_id")
                or str((revision.extracao_bruta or {}).get("aposta_chave") or "").startswith(
                    "web_print:"
                )
            )
        ):
            from uuid import UUID

            from bancaemdia.models.web_print import PrintAsset
            from bancaemdia.services.web_print import photo

            try:
                raw = revision.extracao_bruta or {}
                identity = UUID(raw.get("web_print_id") or str(raw["aposta_chave"]).split(":")[1])
                asset = await session.scalar(
                    select(PrintAsset).where(
                        PrintAsset.usuario_id == usuario_id, PrintAsset.id == identity
                    )
                )
                if asset is None or not any(
                    id_ in result["review_ids"] for result in asset.results or []
                ):
                    return None
                stored = await photo(session, usuario_id, identity)
            except (ValueError, TypeError):
                return None
            if stored is None or stored.hash != revision.midia_hash or stored.content is None:
                return None
            return FotoRevisao(conteudo=stored.content, tipo=stored.mime)
        stmt = (
            select(models.MidiaArquivo.conteudo, models.Midia.tipo)
            .select_from(models.RevisaoPendente)
            .join(models.Midia, models.Midia.hash == models.RevisaoPendente.midia_hash)
            .join(models.MidiaArquivo, models.MidiaArquivo.hash == models.Midia.hash)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.id == id_,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
        )
        linhas = (await session.execute(stmt)).all()
        if not linhas:
            return None
        return FotoRevisao(conteudo=linhas[0][0], tipo=linhas[0][1])

    async def resolve_superseded(
        self,
        session: AsyncSession,
        usuario_id: int,
        aposta_chave: str,
        motivo: str | None,
        *,
        include_account: bool = False,
    ) -> int:
        stmt = update(models.RevisaoPendente).where(
            models.RevisaoPendente.usuario_id == usuario_id,
            models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            models.RevisaoPendente.resolvido_em.is_(None),
            models.RevisaoPendente.motivo.not_in([
                "conta_pendente",
                "consolidacao_pendente",
                "cruzamento_candidato",
            ]),
        )
        if not include_account:
            category = models.RevisaoPendente.extracao_bruta["tipo_revisao"].astext
            stmt = stmt.where(or_(category.is_(None), category != "conta"))
        if motivo is not None:
            stmt = stmt.where(models.RevisaoPendente.motivo != motivo)
        stmt = stmt.values(resolvido_em=func.now()).returning(models.RevisaoPendente.id)
        return len(list((await session.execute(stmt)).scalars()))

    async def has_open(
        self, session: AsyncSession, usuario_id: int, aposta_chave: str, motivo: str
    ) -> bool:
        stmt = (
            select(models.RevisaoPendente.id)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.motivo == motivo,
                models.RevisaoPendente.resolvido_em.is_(None),
                models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def list_by_usuario(
        self, session: AsyncSession, usuario_id: int, apenas_abertas: bool = True
    ) -> list[RevisaoPendente]:
        stmt = select(models.RevisaoPendente).where(models.RevisaoPendente.usuario_id == usuario_id)
        if apenas_abertas:
            stmt = stmt.where(models.RevisaoPendente.resolvido_em.is_(None))
        stmt = stmt.order_by(models.RevisaoPendente.criado_em, models.RevisaoPendente.id)
        return [RevisaoPendente(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def list_abertas_by_aposta_chave(
        self, session: AsyncSession, usuario_id: int, aposta_chave: str
    ) -> list[RevisaoPendente]:
        stmt = (
            select(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.resolvido_em.is_(None),
                models.RevisaoPendente.extracao_bruta["aposta_chave"].astext == aposta_chave,
            )
            .order_by(models.RevisaoPendente.criado_em, models.RevisaoPendente.id)
        )
        return [RevisaoPendente(**colunas(obj)) for obj in (await session.execute(stmt)).scalars()]

    async def resolve(
        self, session: AsyncSession, usuario_id: int, id_: int
    ) -> RevisaoPendente | None:
        stmt = (
            update(models.RevisaoPendente)
            .where(
                models.RevisaoPendente.usuario_id == usuario_id,
                models.RevisaoPendente.id == id_,
                models.RevisaoPendente.resolvido_em.is_(None),
            )
            .values(resolvido_em=func.now())
            .returning(models.RevisaoPendente)
        )
        obj = (await session.execute(stmt)).scalar_one_or_none()
        return None if obj is None else RevisaoPendente(**colunas(obj))
