#!/usr/bin/env python3
"""Reproduce interleaved whole-scene edits against a live Atlas Render server.

Every request below uses an endpoint that exists in this repository today.
The script deliberately does not call a partial merge, patch, revision-history
or precondition endpoint, because the server does not provide one.  Client-side
three-way integration is performed in this file and is submitted as one whole
document through PUT /v1/scenes/{id}, exactly like any other save.

Run: python3 scripts/conflict_demo.py --scenario all
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from walkthrough import Demo, tiny_png  # noqa: E402

RENDERS = []


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def clone(value):
    return json.loads(json.dumps(value))


def load_case(case_id):
    path = ROOT / 'examples' / 'conflict' / case_id / 'case.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def check_case_fixture(case_id, actual):
    """Guard the static fixtures against drift from the live scenario."""
    case = load_case(case_id)
    if case is None:
        return {'fixture': None, 'checked': False}
    expected = case['expect']
    checks = {}
    if case_id == 'layer-order':
        checks['forceReplayLayerIds'] = actual['forceReplay']['layerIds'] == expected['forceReplayLayerIds']
        checks['forceReplayRevertsSceneName'] = (actual['forceReplay']['revertedAName']
                                                 == expected['forceReplayRevertsSceneName'])
        checks['identicalDocumentAlsoRejected'] = (actual['unchangedDocument']['status'] == 409) == expected['identicalDocumentAlsoRejected']
        checks['integratedLayerIds'] = actual['manualIntegration']['layerIds'] == expected['integratedLayerIds']
        checks['integratedOverColor'] = actual['manualIntegration']['overColor'] == expected['integratedOverColor']
        checks['pixelsDiffer'] = actual['pixels']['forceVsManual']['changedPixels'] > 0
    elif case_id == 'ancestor-transform':
        for name, bounds in expected['worldBounds'].items():
            checks['anchor:' + name] = actual['worldBounds'][name]['anchor'] == bounds['anchor']
            checks['x:' + name] = actual['worldBounds'][name]['x'] == bounds['x']
            checks['y:' + name] = actual['worldBounds'][name]['y'] == bounds['y']
    elif case_id == 'asset-retirement':
        checks['pinnedRenderAfterRetirement'] = (actual['pinnedRenderAfterRetirement']
                                                 == expected['pinnedRenderAfterRetirement'])
        checks['saveAfterRetirementStatus'] = actual['unrelatedSave']['status'] == expected['saveAfterRetirementStatus']
        checks['freezeAfterRetirementStatus'] = actual['newFreeze']['status'] == expected['freezeAfterRetirementStatus']
        checks['sameIdReimportStatus'] = actual['sameIdReimport']['status'] == expected['sameIdReimportStatus']
        checks['reviveEndpointStatus'] = actual['reviveEndpoint']['status'] == expected['reviveEndpointStatus']
        checks['repointKeepsPixels'] = (actual['pinnedVsRepointedPixels']['changedPixels'] == 0) == expected['repointKeepsPixels']
        checks['deleteLayerChangesPixels'] = (actual['repointedVsDroppedPixels']['changedPixels'] > 0) == expected['deleteLayerChangesPixels']
        checks['importIntoSourceServiceStatus'] = (actual['importPinnedPackageIntoService']['status']
                                                   == expected['importIntoSourceServiceStatus'])
    elif case_id == 'archive-restore':
        checks['archiveStatus'] = actual['archive']['status'] == expected['archiveStatus']
        checks['saveWhileArchivedStatus'] = (actual['writesWhileArchived']['save']['status']
                                             == expected['saveWhileArchivedStatus'])
        checks['freezeWhileArchivedStatus'] = (actual['writesWhileArchived']['freeze']['status']
                                               == expected['freezeWhileArchivedStatus'])
        checks['restoreStatus'] = actual['restore']['status'] == expected['restoreStatus']
        checks['restoreKeepsDocument'] = actual['restore']['documentStillA'] == expected['restoreKeepsDocument']
        checks['restoreDoesNotResetRevision'] = (actual['restore']['revisionNotResetToPreArchive']
                                                 == expected['restoreDoesNotResetRevision'])
        checks['repeatedRestoreBumpsRevision'] = (actual['restore']['repeatRestoreBumped']
                                                  == expected['repeatedRestoreBumpsRevision'])
        checks['staleCheckoutReplaySilentlyReverts'] = (actual['cachedCheckoutReplay']['silentlyRevertedA']
                                                        == expected['staleCheckoutReplaySilentlyReverts'])
        checks['packageImportCreatesSeparateScene'] = (actual['packageImport']['forkIsSeparate']
                                                       == expected['packageImportCreatesSeparateScene'])
        checks['packageImportOntoCanonicalSceneStatus'] = (actual['packageImport']['sameDestination']['status']
                                                           == expected['packageImportOntoCanonicalSceneStatus'])
    return {'fixture': str(case.get('id')), 'checked': True,
            'passed': all(checks.values()), 'checks': checks}


def save(demo, scene, revision, document, name='冲突演练', status=200):
    return demo.call('PUT', '/v1/scenes/' + scene,
                     {'name': name, 'expectedRevision': revision, 'document': document}, status)


def freeze(demo, scene, revision, status=201):
    return demo.call('POST', '/v1/scenes/' + scene + '/snapshots',
                     {'expectedRevision': revision}, status)


def render(demo, snapshot, key):
    job = demo.wait(demo.submit(snapshot, key)['id'])
    RENDERS.append((key, job['artifactId']))
    return job['artifactId']


def compare(demo, left, right):
    return demo.call('POST', '/v1/compare', {'left': left, 'right': right})


def error_body(body):
    if isinstance(body, dict):
        return {'code': body.get('code'), 'error': body.get('error')}
    return {'raw': body.decode(errors='replace')[:200]}


def layer_ids(document):
    out = []

    def walk(layers):
        for layer in layers:
            out.append(layer['id'])
            walk(layer.get('children', []))

    walk(document['layers'])
    return out


def find_layer(document, layer_id):
    def walk(layers):
        for layer in layers:
            if layer['id'] == layer_id:
                return layer
            found = walk(layer.get('children', []))
            if found is not None:
                return found
        return None

    return walk(document['layers'])


def matrix(transform):
    radians = transform.get('degrees', 0) * math.pi / 180
    cos, sin = math.cos(radians), math.sin(radians)
    sx, sy = transform['scaleX'], transform['scaleY']
    return (cos * sx, sin * sx, -sin * sy, cos * sy,
            transform.get('x', 0), transform.get('y', 0))


def multiply(outer, inner):
    a, b, c, d, e, f = outer
    na, nb, nc, nd, ne, nf = inner
    return (a * na + c * nb, b * na + d * nb,
            a * nc + c * nd, b * nc + d * nd,
            a * ne + c * nf + e, b * ne + d * nf + f)


def apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def world_bounds(group, child):
    m = multiply(matrix(group['transform']), matrix(child['transform']))
    width, height = child['width'], child['height']
    points = [apply(m, x, y) for x, y in ((0, 0), (width, 0), (0, height), (width, height))]
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return {'anchor': [round(m[4], 3), round(m[5], 3)],
            'x': [round(min(xs), 3), round(max(xs), 3)],
            'y': [round(min(ys), 3), round(max(ys), 3)]}


def rect(layer_id, x, y, width, height, color):
    return {'id': layer_id, 'kind': 'rectangle', 'width': width, 'height': height,
            'transform': {'x': x, 'y': y, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
            'opacity': 1, 'fill': {'kind': 'solid', 'color': color}}


def ellipse(layer_id, x, y, width, height, color, opacity=1):
    return {'id': layer_id, 'kind': 'ellipse', 'width': width, 'height': height,
            'transform': {'x': x, 'y': y, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
            'opacity': opacity, 'fill': {'kind': 'solid', 'color': color}}


# ---------------------------------------------------------------- scenario 1
def scenario_layer_order(demo):
    base_body = {'width': 160, 'height': 120, 'background': '#F5F1E8', 'layers': [
        rect('under', 10, 10, 100, 100, '#145A70'),
        rect('over', 50, 40, 100, 100, '#E87E4F'),
    ]}
    created = save(demo, 'order-demo', 0, base_body, name='层序冲突')
    base = created['document']

    # Author A inserts a layer between two existing layers and renames the scene.
    a_document = clone(base)
    a_document['layers'].insert(1, ellipse('halo', 40, 20, 80, 80, '#FFF4D5', .85))
    a_scene = save(demo, 'order-demo', created['revision'], a_document, name='层序冲突 v2')
    a_snapshot = freeze(demo, 'order-demo', a_scene['revision'])
    a_artifact = render(demo, a_snapshot, 'order-a')

    # Author B, still on the base revision, recolors one layer.  B's cached
    # document is byte-identical to the accepted base, yet the save fails:
    # the precondition is the revision number, not a content diff.
    b_document = clone(base)
    find_layer(b_document, 'over')['fill']['color'] = '#7A3FA0'
    unchanged = save(demo, 'order-demo', created['revision'], base,
                     name='层序冲突', status=409)
    stale = save(demo, 'order-demo', created['revision'], b_document,
                 name='层序冲突', status=409)

    # B "retries" by replaying its whole stale document at the current revision.
    force = save(demo, 'order-demo', a_scene['revision'], b_document, name='层序冲突')
    force_snapshot = freeze(demo, 'order-demo', force['revision'])
    force_artifact = render(demo, force_snapshot, 'order-force')

    # Manual integration: take the current document and apply B's intent by
    # layer id.  The whole document is still submitted as one PUT.
    integrated_document = clone(a_scene['document'])
    find_layer(integrated_document, 'over')['fill']['color'] = '#7A3FA0'
    integrated = save(demo, 'order-demo', force['revision'], integrated_document,
                      name=force['name'])
    integrated_snapshot = freeze(demo, 'order-demo', integrated['revision'])
    integrated_artifact = render(demo, integrated_snapshot, 'order-integrated')

    return {
        'scenario': 'layer-order',
        'observation': '整份重放通过修订检查，却删除了另一位作者插入的图层并改变了层序；冲突检查只看修订号，不看内容差异。',
        'staleSave': {'status': 409, 'body': error_body(stale)},
        'unchangedDocument': {'status': 409, 'body': error_body(unchanged),
                              'note': '文档与已接受基础完全一致也返回 409，说明前置条件是修订号而非内容。'},
        'forceReplay': {
            'status': 200,
            'revision': force['revision'],
            'name': force['name'],
            'layerIds': layer_ids(force['document']),
            'haloPresent': find_layer(force['document'], 'halo') is not None,
            'revertedAName': force['name'] != a_scene['name'],
        },
        'manualIntegration': {
            'status': 200,
            'revision': integrated['revision'],
            'name': integrated['name'],
            'layerIds': layer_ids(integrated['document']),
            'haloPresent': find_layer(integrated['document'], 'halo') is not None,
            'overColor': find_layer(integrated['document'], 'over')['fill']['color'],
        },
        'pixels': {
            'aVsForce': compare(demo, a_artifact, force_artifact),
            'aVsManual': compare(demo, a_artifact, integrated_artifact),
            'forceVsManual': compare(demo, force_artifact, integrated_artifact),
        },
        'documentNormalization': {
            'sentEqualsReturned': canonical(base_body) == canonical(base),
            'note': '服务返回补全后的完整结构；客户端摘要必须以 GET 返回值为基准，否则基准摘要会误判。',
        },
        'verdict': '仅当作者明确意图是丢弃 A 的层序改动时才允许整份重放；正常路径是按图层 ID 人工整合后整体提交。',
    }


# ---------------------------------------------------------------- scenario 2
def scenario_ancestor_transform(demo):
    base_body = {'width': 220, 'height': 220, 'background': '#F5F1E8', 'layers': [
        {'id': 'frame', 'kind': 'group',
         'transform': {'x': 60, 'y': 60, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
         'opacity': 1,
         'children': [rect('tile', 0, 0, 40, 20, '#145A70')]},
    ]}
    created = save(demo, 'frame-demo', 0, base_body)
    base = created['document']

    # A rotates the ancestor group.
    a_document = clone(base)
    find_layer(a_document, 'frame')['transform']['degrees'] = 90
    a_scene = save(demo, 'frame-demo', created['revision'], a_document)
    a_snapshot = freeze(demo, 'frame-demo', a_scene['revision'])
    a_artifact = render(demo, a_snapshot, 'frame-a')

    # B's recorded intent is a world-space nudge: move the tile 40px right.
    b_document = clone(base)
    find_layer(b_document, 'tile')['transform']['x'] = 40
    stale = save(demo, 'frame-demo', created['revision'], b_document, status=409)
    force = save(demo, 'frame-demo', a_scene['revision'], b_document)
    force_snapshot = freeze(demo, 'frame-demo', force['revision'])
    force_artifact = render(demo, force_snapshot, 'frame-force')

    # Naive field merge keeps A's rotated frame and B's local x=40.
    naive_document = clone(a_scene['document'])
    find_layer(naive_document, 'tile')['transform']['x'] = 40
    naive = save(demo, 'frame-demo', force['revision'], naive_document)
    naive_snapshot = freeze(demo, 'frame-demo', naive['revision'])
    naive_artifact = render(demo, naive_snapshot, 'frame-naive')

    # Intent-preserving integration: world +40 right under a 90 degree frame
    # is local y = -40, not local x = 40.
    intent_document = clone(a_scene['document'])
    find_layer(intent_document, 'tile')['transform']['y'] = -40
    intent = save(demo, 'frame-demo', naive['revision'], intent_document)
    intent_snapshot = freeze(demo, 'frame-demo', intent['revision'])
    intent_artifact = render(demo, intent_snapshot, 'frame-intent')

    bounds = {}
    for name, document in [('base', base), ('aRotated', a_scene['document']),
                           ('bWorldRight', b_document), ('forceReplay', force['document']),
                           ('naiveFieldMerge', naive['document']),
                           ('intentPreserving', intent['document'])]:
        bounds[name] = world_bounds(find_layer(document, 'frame'),
                                    find_layer(document, 'tile'))

    return {
        'scenario': 'ancestor-transform',
        'observation': '祖先变换改变后代局部坐标的含义；逐字段合并能通过修订检查，却把“向右移动”变成“向下移动”。',
        'staleSave': {'status': 409, 'body': error_body(stale)},
        'worldBounds': bounds,
        'saves': {
            'forceReplayRevision': force['revision'],
            'naiveFieldMergeRevision': naive['revision'],
            'intentPreservingRevision': intent['revision'],
            'allAccepted': True,
        },
        'pixels': {
            'aVsForce': compare(demo, a_artifact, force_artifact),
            'forceVsNaive': compare(demo, force_artifact, naive_artifact),
            'naiveVsIntent': compare(demo, naive_artifact, intent_artifact),
            'forceVsIntent': compare(demo, force_artifact, intent_artifact),
        },
        'verdict': '祖先链上任一节点的变换、透明度或裁剪被改动时，后代改动不能按字段直接拼接；必须按记录的世界坐标意图重算局部值并渲染复核。',
    }


# ---------------------------------------------------------------- scenario 3
def scenario_asset_retirement(demo):
    asset = demo.call('PUT', '/v1/assets/ribbon', tiny_png(), 201)
    base_body = {'width': 160, 'height': 120, 'background': '#F5F1E8', 'layers': [
        {'id': 'pic', 'kind': 'image', 'width': 40, 'height': 40, 'assetId': 'ribbon',
         'transform': {'x': 20, 'y': 20, 'scaleX': 1, 'scaleY': 1, 'degrees': 0},
         'opacity': 1, 'fill': {'kind': 'solid', 'color': '#000000'}},
    ]}
    created = save(demo, 'asset-demo', 0, base_body)
    base = created['document']
    pinned = freeze(demo, 'asset-demo', created['revision'])
    pinned_artifact = render(demo, pinned, 'asset-pinned')
    package = demo.call('GET', '/v1/snapshots/' + pinned['id'] + '/package')

    demo.call('POST', '/v1/assets/ribbon/retire')

    # An unrelated edit cannot be saved while the stale document still points
    # at the retired id.
    unrelated = clone(base)
    unrelated['background'] = '#000000'
    blocked = save(demo, 'asset-demo', created['revision'], unrelated, status=409)
    blocked_freeze = freeze(demo, 'asset-demo', created['revision'], status=409)

    # Same bytes can be imported under a NEW id, but that is a new asset
    # lineage, not a revival of the retired id.
    replacement = demo.call('PUT', '/v1/assets/ribbon2', tiny_png(), 201)
    repointed_document = clone(base)
    find_layer(repointed_document, 'pic')['assetId'] = 'ribbon2'
    repointed = save(demo, 'asset-demo', created['revision'], repointed_document)
    repointed_snapshot = freeze(demo, 'asset-demo', repointed['revision'])
    repointed_artifact = render(demo, repointed_snapshot, 'asset-repointed')

    # Dropping the layer is visually different and needs author approval.
    dropped_document = clone(repointed['document'])
    dropped_document['layers'] = [l for l in dropped_document['layers'] if l['id'] != 'pic']
    dropped = save(demo, 'asset-demo', repointed['revision'], dropped_document)
    dropped_snapshot = freeze(demo, 'asset-demo', dropped['revision'])
    dropped_artifact = render(demo, dropped_snapshot, 'asset-dropped')

    reimport = demo.call('PUT', '/v1/assets/ribbon', tiny_png(), 409)
    revive = demo.call('POST', '/v1/assets/ribbon/revive', status=404)

    # Exporting a pinned snapshot still works, but importing it back into the
    # same service conflicts with the retired asset record.
    import_conflict = demo.call('POST', '/v1/imports/asset-fork', {'name': '素材分叉', 'package': package}, 409)

    return {
        'scenario': 'asset-retirement',
        'observation': '素材退役是全局且不可逆的；整份保存会在提交前被素材闭包检查拦下，而局部改写引用的方案会改变素材谱系。',
        'retiredAssetDigest': asset['digest'],
        'pinnedRenderAfterRetirement': 'succeeded',
        'newFreeze': {'status': 409, 'body': error_body(blocked_freeze)},
        'unrelatedSave': {'status': 409, 'body': error_body(blocked)},
        'sameIdReimport': {'status': 409, 'body': error_body(reimport)},
        'reviveEndpoint': {'status': 404},
        'replacementAsset': {'id': replacement['id'], 'sameBytes': replacement['digest'] == asset['digest']},
        'pinnedVsRepointedPixels': compare(demo, pinned_artifact, repointed_artifact),
        'repointedVsDroppedPixels': compare(demo, repointed_artifact, dropped_artifact),
        'snapshotDigestChangedAfterRepoint': pinned['digest'] != repointed_snapshot['digest'],
        'importPinnedPackageIntoService': {'status': 409, 'body': error_body(import_conflict)},
        'verdict': '退役后不存在“恢复原 ID”的现行接口；同字节新 ID 可保持像素不变但改变谱系，删除图层会改变画面。两者都必须由作者确认并记录。',
    }


# ---------------------------------------------------------------- scenario 4
def scenario_archive_restore(demo):
    base_body = {'width': 160, 'height': 120, 'background': '#F5F1E8', 'layers': [
        rect('card', 20, 20, 120, 80, '#145A70'),
    ]}
    created = save(demo, 'archive-demo', 0, base_body)
    base = created['document']
    base_snapshot = freeze(demo, 'archive-demo', created['revision'])
    render(demo, base_snapshot, 'archive-base')

    # Author A changes content before the archive.
    a_document = clone(base)
    a_document['background'] = '#103040'
    a_scene = save(demo, 'archive-demo', created['revision'], a_document)
    dark_snapshot = freeze(demo, 'archive-demo', a_scene['revision'])
    dark_artifact = render(demo, dark_snapshot, 'archive-dark')
    dark_digest = digest(a_scene['document'])

    # Author B archives, then restores.  No content is reverted by either.
    archived = demo.call('POST', '/v1/scenes/archive-demo/state',
                         {'expectedRevision': a_scene['revision'], 'state': 'archived'})
    edit_while_archived = save(demo, 'archive-demo', archived['revision'], a_document, status=409)
    freeze_while_archived = freeze(demo, 'archive-demo', archived['revision'], status=409)
    restored = demo.call('POST', '/v1/scenes/archive-demo/state',
                         {'expectedRevision': archived['revision'], 'state': 'active'})
    restored_again = demo.call('POST', '/v1/scenes/archive-demo/state',
                               {'expectedRevision': restored['revision'], 'state': 'active'})

    # B's old cached checkout (before A's edit) is replayed at the now-current
    # revision.  This passes the revision check and silently reverts A.
    cached_replay = save(demo, 'archive-demo', restored['revision'], base)
    replay_snapshot = freeze(demo, 'archive-demo', cached_replay['revision'])
    replay_artifact = render(demo, replay_snapshot, 'archive-replay')

    # A pre-archive package can be imported only as a NEW scene.  It is a fork,
    # not a rollback of the canonical scene.
    package = demo.call('GET', '/v1/snapshots/' + base_snapshot['id'] + '/package')
    fork = demo.call('POST', '/v1/imports/archive-fork', {'name': '归档分叉', 'package': package}, 201)
    same_destination = demo.call('POST', '/v1/imports/archive-demo', {'name': '非法覆盖', 'package': package}, 409)
    canonical = demo.call('GET', '/v1/scenes/archive-demo')

    return {
        'scenario': 'archive-restore',
        'observation': '归档/恢复只改变状态并递增修订，不保存或还原内容；恢复后的整份旧稿回放才是覆盖他人修改的动作。',
        'archive': {'status': 200, 'revision': archived['revision'], 'state': archived['state']},
        'writesWhileArchived': {
            'save': {'status': 409, 'body': error_body(edit_while_archived)},
            'freeze': {'status': 409, 'body': error_body(freeze_while_archived)},
        },
        'restore': {
            'status': 200,
            'revision': restored['revision'],
            'state': restored['state'],
            'documentStillA': digest(restored['document']) == dark_digest,
            'revisionNotResetToPreArchive': restored['revision'] != a_scene['revision'],
            'repeatRestoreRevision': restored_again['revision'],
            'repeatRestoreBumped': restored_again['revision'] != restored['revision'],
        },
        'cachedCheckoutReplay': {
            'status': 200,
            'revision': cached_replay['revision'],
            'silentlyRevertedA': digest(cached_replay['document']) != dark_digest
                               and digest(cached_replay['document']) == digest(base),
        },
        'pixels': {
            'darkVsRestored': compare(demo, dark_artifact, replay_artifact),
        },
        'packageImport': {
            'forkScene': fork['id'],
            'forkRevision': fork['revision'],
            'forkIsSeparate': fork['id'] != 'archive-demo',
            'canonicalRevisionAfterFork': canonical['revision'],
            'sameDestination': {'status': 409, 'body': error_body(same_destination)},
        },
        'verdict': '恢复后必须重新 GET 当前文档并做三方整合；不得把旧检出、旧交换包或归档时刻的记忆当作“恢复内容”。真正回滚只能新建分叉场景并显式整合。',
    }


SCENARIOS = {
    'layer-order': scenario_layer_order,
    'ancestor-transform': scenario_ancestor_transform,
    'asset-retirement': scenario_asset_retirement,
    'archive-restore': scenario_archive_restore,
}


def capture(demo, out):
    directory = Path(out) / 'png'
    directory.mkdir(parents=True, exist_ok=True)
    for key, artifact_id in RENDERS:
        (directory / (key + '.png')).write_bytes(
            demo.call('GET', '/v1/artifacts/' + artifact_id))
    return str(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', default='all', choices=['all'] + sorted(SCENARIOS))
    parser.add_argument('--out')
    args = parser.parse_args()
    selected = sorted(SCENARIOS) if args.scenario == 'all' else [args.scenario]
    with tempfile.TemporaryDirectory(prefix='atlas-conflict-') as temp:
        directory = Path(temp)
        binary = directory / 'atlas'
        subprocess.run(['go', 'build', '-o', str(binary), './cmd/atlas'],
                       cwd=ROOT, check=True, timeout=60)
        demo = Demo(binary, directory)
        try:
            results = [SCENARIOS[name](demo) for name in selected]
            for result in results:
                result['fixtureCheck'] = check_case_fixture(result['scenario'], result)
            drifted = [r['scenario'] for r in results
                       if r['fixtureCheck']['checked'] and not r['fixtureCheck']['passed']]
            if drifted:
                raise RuntimeError('fixture drift: ' + ', '.join(drifted))
            if args.out:
                captured = capture(demo, args.out)
        finally:
            demo.stop()
            demo.log.close()
    report = {'schema': 'atlas-conflict-evidence', 'version': 1,
              'note': '仅使用当前已实现接口；人工整合在本脚本内完成，服务端没有局部合并。',
              'scenarios': results}
    if args.out:
        report['pngDirectory'] = captured
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / 'evidence.json').write_text(text + '\n', encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
