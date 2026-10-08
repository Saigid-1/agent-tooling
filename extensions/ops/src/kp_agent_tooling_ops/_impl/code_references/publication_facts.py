"""Ephemeral facts used only while materializing a portable reference generation.

This is the small structural protocol consumed by the existing resolver, not a
persistent graph service. The resulting frozen generation owns serving data.
"""
from copy import deepcopy


class PublicationFacts:
    def __init__(self):
        self.nodes, self.edges = {}, {}
        self.entities = self.relationships = self.adapter = self

    def get(self, identity):
        return deepcopy(self.nodes.get(identity))

    def find(self, kind, attributes):
        return [deepcopy(n) for n in self.nodes.values() if n['entity_type'] == kind
                and all(n['attributes'].get(k) == v for k, v in attributes.items())]

    def create(self, kind, *args, **kwargs):
        if 'entity_id' in kwargs:
            identity = kwargs['entity_id']
            value = {'id': identity, 'entity_type': kind, 'attributes': deepcopy(args[0])}
            store = self.nodes
        else:
            identity = kwargs['edge_id']
            value = {'id': identity, 'relationship_type': kind, 'from_id': args[0],
                     'to_id': args[1], 'attributes': deepcopy(kwargs['attributes'])}
            store = self.edges
        if identity in store and store[identity] != value:
            raise ValueError('immutable publication identity conflict')
        store[identity] = value
        return deepcopy(value)

    def get_edge(self, identity):
        return deepcopy(self.edges.get(identity))
