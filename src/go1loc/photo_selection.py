"""Visible raw-photo ROI editing with Viser's native mouse drag events.

No custom browser scripts or extra HTTP endpoints. Coordinates always refer to
the original image, not the display thumbnail. Selection is bound to its bytes.
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .video import sha256_file

PHOTO_PLANE_Z = 10000.


def checked_roi(roi, width, height):
    box = np.asarray(roi, dtype=float)
    if (box.shape != (4,) or not np.isfinite(box).all()
            or np.any(box[:2] < 0) or np.any(box[2:] > [width, height])
            or np.any(box[2:] - box[:2] < 1)):
        raise ValueError('选框无效：请在原图范围内选择至少 1×1 像素，右下角须大于左上角。')
    return box.tolist()


def world_pixel(point, width, height):
    p = np.asarray(point, dtype=float)
    if p.shape != (3,) or not np.isfinite(p).all():
        raise ValueError('选框无效：鼠标位置不是有限坐标。')
    return np.array([p[0] * height / 2 + width / 2, (1 - p[1]) * height / 2])


def pixel_world(point, width, height, z=PHOTO_PLANE_Z + .006):
    return (2 * (point[0] - width / 2) / height, 1 - 2 * point[1] / height, z)


def gesture_roi(kind, original, anchor, end, width, height):
    """Use the gesture-start anchor, NOT Viser's moving grab point on updates."""
    size = np.array([width, height], dtype=float)
    if kind == 'draw':
        a, b = np.clip(anchor, 0, size), np.clip(end, 0, size)
        return [*np.minimum(a, b), *np.maximum(a, b)]
    box = np.array(original, dtype=float)
    delta = np.asarray(end) - anchor
    if kind == 'move':
        delta = np.clip(delta, -box[:2], size - box[2:])
        return (box + np.tile(delta, 2)).tolist()
    for axis, index in enumerate({'tl': (0, 1), 'tr': (2, 1), 'br': (2, 3), 'bl': (0, 3)}[kind]):
        lo, hi = (0, box[index + 2] - 1) if index < 2 else (box[index - 2] + 1, size[axis])
        box[index] = np.clip(box[index] + delta[axis], lo, hi)
    return box.tolist()


def selection_preview(path, roi):
    with Image.open(path) as source:
        image = source.convert('RGB')
    width, height = image.size
    image.thumbnail((640, 480))
    x0, y0, x1, y1 = roi
    rect = [min(image.width - 1, x0 * image.width / width), min(image.height - 1, y0 * image.height / height),
            min(image.width - 1, x1 * image.width / width), min(image.height - 1, y1 * image.height / height)]
    draw = ImageDraw.Draw(image)
    draw.rectangle(rect, outline='black', width=5)
    draw.rectangle(rect, outline=(50, 255, 110), width=3)
    return np.asarray(image)


class PhotoSelectionControls:
    def _selection_ui(self):
        gui = self.server.gui
        self._roi_selection = None
        self.roi_status = gui.add_text('框选状态', '尚未框选：打开照片后，直接按住左键拖动。', multiline=True, disabled=True)
        self.roi_preview = gui.add_image(np.zeros((2, 2, 3), np.uint8), label='当前选框预览（绿框内为目标）', visible=False)
        self._button('清除选框 / 重新画框', self._clear_roi)
        gui.add_markdown('**直接在照片上左键拖动画框。** 松手后保留绿框；拖四角方块调整大小，拖中心蓝块移动。'
                         '在框外的照片区域拖动可重画。视角歪了可重新打开原图，选框不会丢失。')
        with gui.add_folder('框选坐标（原图像素，可手动调整）', expand_by_default=False):
            self.roi_min = gui.add_vector2('左上角 x / y', (0., 0.))
            self.roi_max = gui.add_vector2('右下角 x / y', (0., 0.))
        self.roi_min.on_update(self._guard(self._manual_roi))
        self.roi_max.on_update(self._guard(self._manual_roi))
        self.photo.on_update(self._guard(self._photo_input_changed))
        self.roi_submit = self._button('计算目标位置与证据', self._locate_object)
        self.roi_submit.disabled = True

    def _photo_input_changed(self, event):
        # Server-side assignments during result loading must not erase that result.
        if getattr(event, 'client_id', None) is None:
            return
        self._invalidate_target(clear_query=True)
        self.query_path.value = ''

    def _remove_photo_state(self, client_id):
        state = self._photo_states.pop(client_id, None)
        if state:
            for handle in state['overlays'].values():
                handle.remove()
            state['handle'].remove()

    def _clear_photo_selection(self):
        for client_id in list(self._photo_states):
            self._remove_photo_state(client_id)
        self._roi_selection = None
        self.roi_submit.disabled = True
        self.roi_preview.visible = False
        self.roi_status.value = '尚未框选：打开照片后，直接按住左键拖动。'
        self.roi_min.value = self.roi_max.value = (0., 0.)

    def _clear_roi(self, _):
        self._invalidate_target(clear_query=False)
        self._roi_selection = None
        for state in self._photo_states.values():
            for handle in state['overlays'].values():
                handle.remove()
            state['overlays'].clear()
            state['drag'] = None
        self.roi_submit.disabled = True
        self.roi_preview.visible = False
        self.roi_min.value = self.roi_max.value = (0., 0.)
        self.roi_status.value = '尚未框选：请在照片上按住左键拖出矩形。'

    def _reject_roi(self, message):
        self._invalidate_target(clear_query=False)
        self._roi_selection = None
        for state in self._photo_states.values():
            state['drag'] = None
            for handle in state['overlays'].values():
                handle.visible = False
        self.roi_submit.disabled = True
        self.roi_preview.visible = False
        self.roi_status.value = '选框无效：' + str(message)

    def _open_photo(self, event):
        from .viser_viewer import _read_image
        self._pause_playback()
        if event.client is None:
            raise ValueError('photo_selection_requires_browser_client')
        path = Path(self.photo.value).resolve()
        with Image.open(path) as image:
            width, height = image.size
        digest = sha256_file(path)
        selection = self._roi_selection
        if selection and (selection['path'] != path or selection['sha256'] != digest):
            self._invalidate_target(clear_query=True)
            self.query_path.value = ''
            selection = None
        # A different browser may still display another photo. Retire its handles.
        for cid, old in list(self._photo_states.items()):
            if old['path'] != path or old['sha256'] != digest:
                self._remove_photo_state(cid)
        client = event.client
        self._remove_photo_state(client.client_id)
        handle = client.scene.add_image('/roi/photo', _read_image(path, 1600),
            render_width=2. * width / height, render_height=2., position=(0., 0., PHOTO_PLANE_Z),
            wxyz=(0., 1., 0., 0.), cast_shadow=False, receive_shadow=False)
        state = {'path': path, 'width': width, 'height': height, 'sha256': digest,
                 'handle': handle, 'overlays': {}, 'client': client, 'drag': None}
        self._photo_states[client.client_id] = state
        self._bind_roi_drag(handle, state, 'draw')
        client.camera.up_direction = (0., 1., 0.)
        distance = 1.12 * max(1., width / height / max(.1, client.camera.aspect)) / np.tan(.9 / 2)
        client.camera.position = (0., 0., PHOTO_PLANE_Z + distance)
        client.camera.look_at = (0., 0., PHOTO_PLANE_Z)
        client.camera.wxyz = (0., 1., 0., 0.)
        client.camera.fov = .9
        if selection:
            self._draw_roi(state, selection['roi'], handles=True)
        else:
            self.roi_status.value = '尚未框选：在照片上按住鼠标左键拖动，松手即可选中。'
            self.roi_submit.disabled = True
        self._message('照片已打开。直接左键拖动画框；松手保留绿框，四角调整大小，中心蓝块移动。')

    def _draw_roi(self, state, roi, *, handles):
        w, h = state['width'], state['height']
        x0, y0, x1, y1 = roi
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        points = np.asarray([pixel_world(p, w, h) for p in corners], dtype=np.float32)
        lines = np.stack([points, np.roll(points, -1, axis=0)], axis=1)
        overlay, scene = state['overlays'], state['client'].scene
        if 'outline' not in overlay:
            overlay['outline'] = scene.add_line_segments('/roi/outline', points=lines,
                colors=(50, 255, 110), thickness=3, thickness_units='screen')
        else:
            overlay['outline'].points = lines
            overlay['outline'].visible = True
        if not handles:
            return
        # Small flat squares, not a 3D translation/rotation gizmo.
        size = min(.045, max(.012, min(x1 - x0, y1 - y0) / h * .3))
        for key, p in zip(('tl', 'tr', 'br', 'bl', 'move'), corners + [((x0+x1)/2, (y0+y1)/2)]):
            position = pixel_world(p, w, h, PHOTO_PLANE_Z + .015)
            if key not in overlay:
                box = scene.add_box('/roi/handle-' + key, dimensions=(size, size, .004),
                    color=(40, 160, 255) if key == 'move' else (50, 255, 110),
                    position=position, cast_shadow=False, receive_shadow=False)
                self._bind_roi_drag(box, state, key)
                overlay[key] = box
            else:
                overlay[key].position = position
                overlay[key].dimensions = (size, size, .004)
                overlay[key].visible = True

    def _bind_roi_drag(self, handle, state, kind):
        # Async with no await keeps start/update/end ordered on Viser's event loop.
        @handle.on_drag
        async def dragged(event):
            with self._state_lock:
                if self._photo_states.get(event.client_id) is not state:
                    return  # late event from a retired photo/handle
                try:
                    if Path(self.photo.value).resolve() != state['path']:
                        raise ValueError('照片已变化，请重新打开原图后框选。')
                    q = np.asarray(event.client.camera.wxyz)
                    if not (np.allclose(q, (0, 1, 0, 0), atol=1e-4) or np.allclose(q, (0, -1, 0, 0), atol=1e-4)):
                        raise ValueError('照片视角已倾斜，请点击「打开原图并框选目标」恢复。')
                    w, h = state['width'], state['height']
                    if event.phase == 'start':
                        original = self._roi_selection['roi'] if self._roi_selection else None
                        if kind != 'draw' and original is None:
                            return
                        self._invalidate_target(clear_query=False)
                        state['drag'] = (kind, original, world_pixel(event.start_position, w, h), self._task_revision)
                        self._roi_selection = None
                        self.roi_submit.disabled = True
                        self.roi_preview.visible = False
                        if kind == 'draw':
                            for key in list(state['overlays']):
                                if key != 'outline':
                                    state['overlays'].pop(key).remove()
                    if not state['drag']:
                        return
                    gesture, original, anchor, revision = state['drag']
                    if revision != self._task_revision:
                        state['drag'] = None
                        return  # another tab/input superseded this unfinished gesture
                    roi = gesture_roi(gesture, original, anchor, world_pixel(event.end_position, w, h), w, h)
                    self._draw_roi(state, roi, handles=kind != 'draw')
                    self.roi_status.value = '正在框选 / 调整…松开鼠标后确认。'
                    if event.phase == 'end':
                        state['drag'] = None
                        self._select_roi(state, roi)
                except Exception as exc:
                    self._reject_roi(exc)
                    self._message(str(exc))

    def _select_roi(self, state, roi):
        if (Path(self.photo.value).resolve() != state['path']
                or sha256_file(state['path']) != state['sha256']):
            raise ValueError('照片或照片内容已变化，请重新打开原图并框选。')
        roi = checked_roi(roi, state['width'], state['height'])
        self._invalidate_target(clear_query=False)
        self._roi_selection = {k: state[k] for k in ('path', 'sha256', 'width', 'height')}
        self._roi_selection['roi'] = roi
        self.roi_min.value, self.roi_max.value = tuple(roi[:2]), tuple(roi[2:])
        for other in self._photo_states.values():
            if other['path'] == state['path'] and other['sha256'] == state['sha256']:
                self._draw_roi(other, roi, handles=True)
        self.roi_preview.image = selection_preview(state['path'], roi)
        self.roi_preview.visible = True
        self.roi_submit.disabled = False
        self.roi_status.value = ('已选中（尚未提交计算）\n'
            f'原图区域：({roi[0]:.0f}, {roi[1]:.0f}) → ({roi[2]:.0f}, {roi[3]:.0f})\n'
            f'宽 {roi[2]-roi[0]:.0f} × 高 {roi[3]-roi[1]:.0f} 像素。可拖四角或中心蓝块调整。')

    def _manual_roi(self, event):
        if getattr(event, 'client_id', None) is None:
            return
        state = self._photo_states.get(event.client_id)
        try:
            if not state:
                raise ValueError('请先打开原图，再调整选框坐标。')
            self._select_roi(state, [*self.roi_min.value, *self.roi_max.value])
        except ValueError as exc:
            self._reject_roi(exc)
            raise

    def _validated_selection(self):
        selection = self._roi_selection
        if selection is None:
            raise ValueError('请先打开目标原图并框选；看到绿框和「已选中」后再计算。')
        path = Path(self.photo.value).resolve()
        try:
            digest = sha256_file(path)
        except OSError as exc:
            self._reject_roi('照片文件不可读，请重新选择照片。')
            raise ValueError('照片文件不可读，请重新选择照片。') from exc
        if path != selection['path'] or digest != selection['sha256']:
            self._clear_roi(None)
            raise ValueError('照片或照片内容已变化，请重新打开原图并框选。')
        roi = checked_roi([*self.roi_min.value, *self.roi_max.value], selection['width'], selection['height'])
        if not np.allclose(roi, selection['roi'], rtol=0, atol=1e-8):
            raise ValueError('选框坐标与可见框不一致，请重新框选或确认坐标。')
        return str(path), roi
