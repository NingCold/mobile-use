from mobile_use.state import inspect_tree, localized_passive_updates, overlaps_regions


def tree(seconds="10", post="A", dot="[900,1197][915,1208]", clickable="false", extra=""):
    return inspect_tree(f'''<hierarchy>
    <node class="android.widget.TextView" text="{seconds}" bounds="[599,2124][650,2175]"/>
    <node class="android.widget.TextView" text="{post}" clickable="{clickable}" bounds="[102,1023][978,1124]"/>
    <node class="android.widget.ImageView" resource-id="app:id/dot" bounds="{dot}"/>
    {extra}</hierarchy>''',1080,2400)


def test_combined_passive_changes_bound_each_region():
    regions=localized_passive_updates(tree(),tree("09","B","[910,1197][925,1208]"))
    assert regions and len(regions)==3
    assert overlaps_regions((600,2124,640,2160),regions)
    assert not overlaps_regions((0,121,124,245),regions)


def test_changed_interactive_card_fails_closed():
    assert localized_passive_updates(tree(clickable="true"),tree("09","B",clickable="true")) is None


def test_structural_and_large_motion_fail_closed():
    assert localized_passive_updates(tree(),tree(extra='<node class="android.widget.TextView" text="Pay" bounds="[20,20][100,100]"/>')) is None
    assert localized_passive_updates(tree(),tree(dot="[100,300][1000,1900]")) is None
