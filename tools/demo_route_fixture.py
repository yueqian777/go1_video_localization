"""Export and plot the explicitly synthetic route success/rejection fixture."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_planning import fixture, inputs, NOW
from go1loc.evidence import write_json
from go1loc.planning import build_route_graph, plan_route


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError('Use a new output directory')
    manifest,refs,frames=fixture()
    graph=build_route_graph(manifest,refs,frames)
    start,target=inputs(graph)
    success=plan_route(graph,start,target,now=NOW)
    unreachable=deepcopy(target); unreachable['supporting_frame_ids']=['wall.png']
    rejected=plan_route(graph,start,unreachable,now=NOW)
    unattached=deepcopy(start); unattached['node_id']='not_a_recorded_node'
    no_start=plan_route(graph,unattached,target,now=NOW)
    for name,value in [('route_graph',graph),('start',start),('target',target),
                       ('planning_success',success),('planning_no_path',rejected),('planning_no_start',no_start)]:
        write_json(args.output/(name+'.json'),value)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(9,6),constrained_layout=True)
    ax.set_title('Synthetic module test ONLY - not Go1 field data\nDirected historical graph; execution_permitted=false')
    path=success['path']
    ax.plot([p[0] for p in path],[p[1] for p in path],'-o',color='#187a65',lw=3,label='verified fixture edges (4 map_units)')
    for a,b in zip(path,path[1:]):
        ax.annotate('',xy=b[:2],xytext=a[:2],arrowprops={'arrowstyle':'->','color':'#187a65','lw':2})
    ax.scatter([.01],[0],marker='x',s=140,color='#ce4839',label='nearby but disconnected: no_path')
    ax.annotate('Manual exact start',(0,0),xytext=(-.4,-.32))
    ax.annotate('Target observation camera\nNOT object surface',(2,2),xytext=(1.3,2.2))
    ax.set(xlabel='X [map_units]',ylabel='Y [map_units]',xlim=(-.6,2.7),ylim=(-.5,2.7))
    ax.set_aspect('equal'); ax.grid(alpha=.2); ax.legend(loc='lower right',fontsize=8)
    fig.savefig(args.output/'route_preview.png',dpi=140); plt.close(fig)
    print(success['status'], rejected['failure_reason'], no_start['failure_reason'])


if __name__=='__main__':
    main()
