from django import template

register = template.Library()

@register.filter
def get_item(d, key):
    if not d:
        return ""
    return d.get(str(key), "")
