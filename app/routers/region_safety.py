from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models import BlocklistEntry, SafetyReport, User
from app.pagination import paginate
from app.schemas import BlocklistUserDto, Paginated, RegionCityDto, ReportSummaryDto, SubmitReportRequest, RegionDto
from app.serializers import iso

router = APIRouter(tags=["region-safety"])

# Keep in sync with Frontend/src/data/region.ts (primary + otherAreas merged for API)
REGION_DATA: list[RegionDto] = [
    RegionDto(
        state="NSW",
        stateName="新南威尔士州",
        cities=[
            RegionCityDto(
                name="Sydney",
                cn="悉尼",
                areas=[
                    "Chatswood", "Eastwood", "Hurstville", "Burwood", "Rhodes", "Epping",
                    "Haymarket", "Parramatta", "Ashfield", "Campsie", "Auburn", "Zetland",
                ],
            ),
        ],
    ),
    RegionDto(
        state="VIC",
        stateName="维多利亚州",
        cities=[
            RegionCityDto(
                name="Melbourne",
                cn="墨尔本",
                areas=[
                    "Box Hill", "Glen Waverley", "Clayton", "Doncaster", "Melbourne CBD",
                    "Southbank", "Carlton", "Burwood", "Richmond", "Docklands", "Footscray",
                    "Hawthorn", "Preston", "Online", "Melbourne East", "Monash",
                ],
            ),
            RegionCityDto(
                name="Geelong",
                cn="吉朗",
                areas=["Geelong CBD", "Waurn Ponds", "Waterfront", "Newtown", "Belmont", "Grovedale"],
            ),
        ],
    ),
    RegionDto(
        state="QLD",
        stateName="昆士兰州",
        cities=[
            RegionCityDto(
                name="Brisbane",
                cn="布里斯班",
                areas=[
                    "Sunnybank", "Robertson", "South Brisbane", "St Lucia", "Toowong",
                    "Fortitude Valley", "Garden City",
                ],
            ),
            RegionCityDto(
                name="Gold Coast",
                cn="黄金海岸",
                areas=["Southport", "Surfers Paradise", "Broadbeach", "Robina"],
            ),
        ],
    ),
    RegionDto(
        state="WA",
        stateName="西澳大利亚州",
        cities=[
            RegionCityDto(
                name="Perth",
                cn="珀斯",
                areas=[
                    "Cannington", "Willetton", "Victoria Park", "Perth CBD", "Northbridge",
                    "Morley", "Subiaco",
                ],
            ),
        ],
    ),
    RegionDto(
        state="SA",
        stateName="南澳大利亚州",
        cities=[
            RegionCityDto(
                name="Adelaide",
                cn="阿德莱德",
                areas=[
                    "Adelaide CBD", "Chinatown", "Mawson Lakes", "Glen Osmond 附近",
                    "Norwood", "Prospect",
                ],
            ),
        ],
    ),
    RegionDto(
        state="ACT",
        stateName="澳洲首都领地",
        cities=[
            RegionCityDto(
                name="Canberra",
                cn="堪培拉",
                areas=["City", "Dickson", "Belconnen", "Gungahlin", "Woden", "Tuggeranong"],
            ),
        ],
    ),
    RegionDto(
        state="TAS",
        stateName="塔斯马尼亚州",
        cities=[
            RegionCityDto(
                name="Hobart",
                cn="霍巴特",
                areas=["Sandy Bay", "Hobart CBD", "North Hobart", "Battery Point"],
            ),
        ],
    ),
    RegionDto(
        state="NT",
        stateName="北领地",
        cities=[
            RegionCityDto(
                name="Darwin",
                cn="达尔文",
                areas=["Darwin CBD", "Palmerston", "Casuarina"],
            ),
        ],
    ),
]


@router.get("/regions", response_model=list[RegionDto])
def get_regions():
    return REGION_DATA


@router.get("/safety/reports", response_model=Paginated[ReportSummaryDto])
def list_reports(
    page: int = Query(1, ge=1),
    pageSize: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    q = db.query(SafetyReport).filter(SafetyReport.reporter_id == user.id).order_by(SafetyReport.created_at.desc())
    total = q.count()
    reports = q.offset((page - 1) * pageSize).limit(pageSize).all()
    items = [
        ReportSummaryDto(id=r.id, targetType=r.target_type, status=r.status, createdAt=iso(r.created_at))
        for r in reports
    ]
    return paginate(items, page, pageSize, total)


@router.post("/safety/reports", status_code=204)
def submit_report(body: SubmitReportRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    report = SafetyReport(
        reporter_id=user.id,
        target_type=body.targetType,
        target_id=body.targetId,
        reason=body.reason,
        details=body.details,
    )
    db.add(report)
    db.commit()
    return Response(status_code=204)


@router.get("/safety/blocklist", response_model=list[BlocklistUserDto])
def get_blocklist(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    entries = db.query(BlocklistEntry).filter(BlocklistEntry.blocker_id == user.id).all()
    result = []
    for e in entries:
        blocked = db.query(User).filter(User.id == e.blocked_id).first()
        if blocked:
            result.append(BlocklistUserDto(userId=blocked.id, nickname=blocked.nickname))
    return result


@router.post("/safety/blocklist/{target_user_id}", status_code=204)
def block_user(target_user_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if target_user_id == user.id:
        raise HTTPException(status_code=400, detail={"code": "INVALID_STATE", "message": "Cannot block yourself", "details": {}})
    target = db.query(User).filter(User.id == target_user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "User not found", "details": {}})
    existing = db.query(BlocklistEntry).filter(BlocklistEntry.blocker_id == user.id, BlocklistEntry.blocked_id == target_user_id).first()
    if not existing:
        db.add(BlocklistEntry(blocker_id=user.id, blocked_id=target_user_id))
        db.commit()
    return Response(status_code=204)
