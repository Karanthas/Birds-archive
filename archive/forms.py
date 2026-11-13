import uuid
import re
from datetime import date, time
from django import forms
from archive.services.images import parse_offset


class PersonalNoteForm(forms.Form):
    notes = forms.CharField(label='备注', required=False, max_length=10000, strip=False,
        widget=forms.Textarea(attrs={'rows': 4, 'data-note-draft': ''}))


class ArchivedLocationForm(forms.Form):
    user_location = forms.CharField(label='拍摄地点（留空为未知）', required=False,
        max_length=200, strip=True)


class PersonalBatchForm(forms.Form):
    action = forms.ChoiceField(label='批量整理操作', choices=[('species', '更正鸟种'),
        ('set_favorite', '设置收藏'), ('set_rating', '设置评分'), ('set_location', '设置地点')])
    favorite = forms.ChoiceField(label='收藏目标', required=False,
        choices=[('true', '收藏'), ('false', '取消收藏')])
    rating = forms.TypedChoiceField(label='评分目标', required=False, coerce=int, empty_value=None,
        choices=[('', '清除评分')] + [(str(n), f'{n} 星') for n in range(1, 6)])
    user_location = forms.CharField(label='地点目标（留空为清除）', required=False, max_length=200)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('action') == 'set_favorite' and not cleaned.get('favorite'):
            raise forms.ValidationError('请明确选择收藏或取消收藏。')
        return cleaned

    def management_payload(self):
        action = self.cleaned_data['action']
        return {'set_favorite': lambda: {'favorite': self.cleaned_data['favorite'] == 'true'},
            'set_rating': lambda: {'rating': self.cleaned_data['rating']},
            'set_location': lambda: {'user_location': self.cleaned_data['user_location']}}[action]()


class BatchSpeciesForm(forms.Form):
    """Separate from the current-page location contract; repeated explicit IDs only."""
    photo_ids = forms.CharField(required=False, widget=forms.HiddenInput)
    species_ids = forms.CharField(required=False, widget=forms.HiddenInput)
    local_names = forms.CharField(required=False, widget=forms.HiddenInput)

    def clean(self):
        from archive.services.batch_review import explicit_ids, _manual_set
        cleaned = super().clean()
        def values(key):
            value = self.data.getlist(key) if hasattr(self.data, 'getlist') else self.data.get(key, [])
            return [value] if isinstance(value, str) else value
        try:
            cleaned['photo_ids'] = explicit_ids(values('photo_ids'))
            cleaned['species_ids'], cleaned['local_names'] = _manual_set(values('species_ids'), values('local_names'))
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None
        return cleaned


class BatchLocationForm(forms.Form):
    photo_ids = forms.CharField(required=False, widget=forms.HiddenInput)
    location = forms.CharField(label='地点', max_length=200, strip=True,
                              error_messages={'required':'请填写地点。','max_length':'地点不能超过 200 个字。'})
    filter = forms.ChoiceField(choices=[('all','全部'),('pending','待处理'),('running','处理中'),
                               ('review','待确认'),('failed','失败'),('duplicate','重复')],
                               initial='all',required=False,widget=forms.HiddenInput,
                               error_messages={'invalid_choice':'筛选无效，请重新选择。'})
    page = forms.IntegerField(min_value=1,initial=1,required=False,widget=forms.HiddenInput,
                             error_messages={'invalid':'页码无效。','min_value':'页码无效。'})

    def clean_photo_ids(self):
        raw = self.data.getlist('photo_ids') if hasattr(self.data,'getlist') else self.data.get('photo_ids',[])
        if isinstance(raw,str):
            raw = [raw]
        try:
            ids = list(dict.fromkeys(uuid.UUID(value) for value in raw))
        except (ValueError,TypeError,AttributeError):
            raise forms.ValidationError('选择无效，请重新选择照片。') from None
        if not ids or len(ids)>24:
            raise forms.ValidationError('请选择本页 1–24 张照片。')
        return ids

    def clean_filter(self):
        return self.cleaned_data.get('filter') or 'all'

    def clean_page(self):
        return self.cleaned_data.get('page') or 1


class MissingCaptureTimeForm(forms.Form):
    date = forms.CharField(label='日期',widget=forms.DateInput(attrs={'type':'date'}),
                          error_messages={'required':'请填写日期。'})
    time = forms.CharField(label='时间（可选）',required=False,widget=forms.TimeInput(attrs={'type':'time','step':'1'}))
    offset = forms.CharField(label='UTC 偏移（可选）',required=False,max_length=6,
                            widget=forms.TextInput(attrs={'placeholder':'+08:00'}),
                            error_messages={'max_length':'偏移须为 ±HH:MM，范围 ±14:00。'})
    version = forms.IntegerField(min_value=1,required=False,widget=forms.HiddenInput,
                                error_messages={'invalid':'版本无效，请重新预览。','min_value':'版本无效，请重新预览。'})

    def clean_date(self):
        value=self.cleaned_data['date']
        try:
            if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value):
                raise ValueError
            return date.fromisoformat(value).isoformat()
        except ValueError:
            raise forms.ValidationError('请输入有效日期。') from None

    def clean_time(self):
        value=self.cleaned_data['time']
        if not value:
            return ''
        try:
            if not re.fullmatch(r'[0-9]{2}:[0-9]{2}(:[0-9]{2})?',value):
                raise ValueError
            return time.fromisoformat(value).isoformat(timespec='seconds')
        except ValueError:
            raise forms.ValidationError('请输入有效时间。') from None

    def clean_offset(self):
        value=self.cleaned_data['offset']
        try:
            return parse_offset(value) if value else None
        except ValueError:
            raise forms.ValidationError('偏移须为 ±HH:MM，范围 ±14:00。') from None

    def capture_value(self):
        clock=self.cleaned_data['time']
        return {'local':self.cleaned_data['date']+(' '+clock if clock else ''),
                'precision':'second' if clock else 'date','offset':self.cleaned_data['offset']}


class CaptureCorrectionForm(MissingCaptureTimeForm):
    mode = forms.ChoiceField(label='时间来源', choices=[('manual', '填写时间'),
        ('unknown', '时间未知'), ('inherit', '恢复原始读取值')], initial='manual')
    date = forms.CharField(label='日期', required=False,
        widget=forms.DateInput(attrs={'type': 'date'}))
    time = forms.CharField(label='时间（可选，支持小数秒）', required=False, max_length=41)

    def clean_date(self):
        if self.data.get('mode', 'manual') != 'manual':
            return ''
        if not self.cleaned_data['date']:
            raise forms.ValidationError('请填写日期。')
        return super().clean_date()

    def clean_time(self):
        from archive.models import capture_sort_key
        value = self.cleaned_data['time']
        if self.data.get('mode', 'manual') != 'manual' or not value:
            return ''
        if re.fullmatch(r'[0-9]{2}:[0-9]{2}', value):
            value += ':00'
        try:
            capture_sort_key('2000-01-01 '+value)
        except ValueError:
            raise forms.ValidationError('请输入有效时间，小数秒最多32位。') from None
        return value

    def capture_value(self):
        if self.cleaned_data['mode'] != 'manual':
            return None
        value = super().capture_value()
        if '.' in self.cleaned_data['time']:
            value['precision'] = 'subsecond'
        return value

    def management_payload(self):
        return {'mode': self.cleaned_data['mode'], 'capture': self.capture_value()}


class GalleryFilterForm(forms.Form):
    species = forms.MultipleChoiceField(label='鸟种（任一种）', required=False,
        widget=forms.CheckboxSelectMultiple)
    favorite = forms.ChoiceField(label='收藏', required=False,
        choices=[('all','全部'),('yes','已收藏'),('no','未收藏')])
    rating_min = forms.TypedChoiceField(label='最低星级', required=False, coerce=int,
        choices=[('','全部')]+[(str(i),f'{i} 星及以上') for i in range(1,6)])
    unrated = forms.BooleanField(label='仅未评分', required=False)
    date_from = forms.DateField(label='起始日期', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    date_to = forms.DateField(label='结束日期（含当天）', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    time_unknown = forms.BooleanField(label='仅时间未知', required=False)
    location = forms.CharField(label='地点包含', required=False, max_length=200)
    location_unknown = forms.BooleanField(label='仅地点未知', required=False)

    def __init__(self, *args, **kwargs):
        from archive.models import Species
        from django.db.models import Q
        super().__init__(*args, **kwargs)
        selected = self.data.getlist('species') if hasattr(self.data, 'getlist') else self.data.get('species', [])
        selected = [value for value in selected if isinstance(value, str) and re.fullmatch(
            r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', value)]
        self.fields['species'].choices = [(str(item.pk), item.display_name) for item in
            Species.objects.filter(Q(confirmed_photos__photo__archive_completion__isnull=False) | Q(pk__in=selected)).distinct()]

    def clean(self):
        data = super().clean()
        if hasattr(self.data, 'getlist') and any(len(self.data.getlist(key)) > 1
                for key in self.fields if key != 'species'):
            raise forms.ValidationError('筛选参数不能重复。')
        if data.get('rating_min') and data.get('unrated'):
            raise forms.ValidationError('最低星级与未评分不能同时选择。')
        if data.get('time_unknown') and (data.get('date_from') or data.get('date_to')):
            raise forms.ValidationError('时间未知不能与日期范围同时选择。')
        if data.get('location_unknown') and data.get('location'):
            raise forms.ValidationError('地点未知不能与地点文字同时选择。')
        if data.get('date_from') and data.get('date_to') and data['date_from'] > data['date_to']:
            raise forms.ValidationError('起始日期不能晚于结束日期。')
        return data

    def normalized_filters(self):
        data = self.cleaned_data
        return {key: (sorted(set(value)) if key == 'species' else value.isoformat()
            if key in ('date_from','date_to') else value)
            for key, value in data.items() if value and not (key == 'favorite' and value == 'all')}

    def summary(self):
        labels = []
        for key, value in self.normalized_filters().items():
            if key == 'species':
                names = dict(self.fields['species'].choices)
                labels.append('鸟种：'+'、'.join(names[item] for item in value))
            elif key == 'favorite':
                labels.append('已收藏' if value == 'yes' else '未收藏')
            elif key == 'rating_min':
                labels.append(f'{value} 星及以上')
            elif isinstance(value, bool):
                labels.append(str(self.fields[key].label))
            else:
                labels.append(f'{self.fields[key].label}：{value}')
        return ' · '.join(labels) or '全部照片'


class SpeciesCorrectionForm(forms.Form):
    mode = forms.ChoiceField(label='操作', choices=[('replace', '统一替换'), ('add', '添加'), ('remove', '移除')])
    classification = forms.ChoiceField(label='分类', choices=[('identified', '指定鸟种'), ('unidentified', '明确无法确定')])
    def clean(self):
        from archive.services.management import species_payload
        cleaned = super().clean()
        if self.errors:
            return cleaned
        def values(key):
            raw = self.data.getlist(key) if hasattr(self.data, 'getlist') else self.data.get(key, [])
            return [raw] if isinstance(raw, str) else raw
        try:
            cleaned['payload'] = species_payload({**cleaned, 'species_ids': values('species_ids'),
                'local_names': values('local_names')})
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None
        return cleaned
